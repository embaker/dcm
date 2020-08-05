'''This sub-package captures all of the data storage abstractions

We want to handle data stored in naive local directories or remote network
repositories in a seamless manner, while still allowing users to take advantage
of their specific capabilities.
'''
from __future__ import annotations
import os, enum, logging, asyncio
from contextlib import asynccontextmanager
from functools import partial
from typing import (Optional, AsyncIterator, Tuple, List, Iterable, Union,
                    Dict, TypeVar, Generic, Any, Type)
from typing_extensions import Protocol, runtime_checkable

import janus
import pydicom
from pydicom import Dataset

from ..query import QueryLevel, QueryResult, uid_elems
from ..net import (DcmNode, DicomOpReport, IncomingDataReport,
                   IncomingDataError, IncomingErrorType, RetrieveReport)
from ..report import Report, MultiReport, MultiListReport, ProgressHookBase
from ..util import aclosing, PathInputType


log = logging.getLogger(__name__)


class TransferMethod(enum.Enum):
    '''Enmerate different methods of transferring data'''

    PROXY = enum.auto()
    '''Data is read into memory and then forwarded to any dests

    This is the required transfer method for any filtering/validation
    '''

    REMOTE_COPY = enum.auto()
    '''Data is remotely copied from the src directly to the dest

    This includes the DICOM 'Move-SCU' operation which is a misnomer as it
    really copies the data.
    '''

    LOCAL_COPY = enum.auto()
    '''Data is locally copied from src directly to dest'''

    MOVE = enum.auto()
    '''Data is locally moved so it no longer exists on the src'''

    LINK = enum.auto()
    '''Data is locally hard linked from the src to the dest'''

    SYMLINK = enum.auto()
    '''Data is locally symlinked from the src to the dest'''


class NoValidTransferMethodError(Exception):
    '''Error raised when we are unable to select a valid transfer method'''


# TODO: Need some way of setting `keep_errors` in the chunks
class DataChunk(Protocol):
    '''Most basic protocol for a naive chunk of data

    Can just provide a sequence of data sets'''

    report: IncomingDataReport

    keep_errors: Tuple[IncomingErrorType, ...] = tuple()

    description: Optional[str] = None

    @property
    def n_expected(self) -> Optional[int]:
        raise NotImplementedError

    async def gen_data(self) -> AsyncIterator[Dataset]:
        '''Generator produces the data sets in this chunk'''
        raise NotImplementedError
        yield


class RepoChunk(DataChunk, Protocol):
    '''Smarter chunk of data referencing a DataRepo/QueryResult combo'''

    repo : 'DataRepo[Any, Any, Any, Any]'
    
    qr: QueryResult

    @property
    def n_expected(self) -> Optional[int]:
        return self.qr.n_instances()

    async def gen_data(self) -> AsyncIterator[Dataset]:
        self.report.keep_errors = self.keep_errors
        async with aclosing(self.repo.retrieve(self.qr, report=self.report)) as rgen:
            async for data_set in rgen:
                yield data_set


class DcmNetChunk(RepoChunk):
    '''Repo chunk from a DICOM network node'''
    report: RetrieveReport

    def __init__(self, 
                 repo: 'DcmRepo', 
                 qr: QueryResult, 
                 description: Optional[str] = None):
        self.repo = repo
        self.qr = qr
        self.description = description
        if description is None:
            rep_descr = None
        else:
            rep_descr = description + '-retrieve'
        self.report = RetrieveReport(description=rep_descr, n_expected=self.n_expected)

    def __repr__(self) -> str:
        return f'DcmNetChunk({self.repo}, {self.qr})'

    def __str__(self) -> str:
        return f'({self.repo}) {self.qr}'


class LocalIncomingDataError(IncomingDataError):
    '''Captures errors detected in incoming data stream'''
    def __init__(self,
                 inconsistent: List[Tuple[str, ...]],
                 duplicate: List[Tuple[str, ...]],
                 invalid: List[PathInputType]):
        self.inconsistent = inconsistent
        self.duplicate = duplicate
        self.invalid = invalid

    def __str__(self) -> str:
        res = ['LocalIncomingDataError:']
        for err_type in ('inconsistent', 'duplicate', 'invalid'):
            errors = getattr(self, err_type)
            if errors is None:
                continue
            n_errors = len(errors)
            if n_errors != 0:
                res.append('%d %s,' % (n_errors, err_type))
        return ' '.join(res)


class LocalIncomingReport(IncomingDataReport):
    '''Track incoming data from a local filesystem'''

    def __init__(self, 
                 description: Optional[str] = None, 
                 n_expected: Optional[int] = None,
                 prog_hook: Optional[ProgressHookBase[Any]] = None,
                 keep_errors: Union[bool, Tuple[IncomingErrorType, ...]] = False,
                 ):
        super().__init__(description, n_expected, prog_hook, keep_errors)
        self.invalid: List[PathInputType] = []
    '''Track any paths that were determined to not be valid DICOM files'''

    @property
    def n_input(self) -> int:
        return super().n_input + len(self.invalid)

    @property
    def n_errors(self) -> int:
        return super().n_errors + len(self.invalid)

    def add_invalid(self, path: PathInputType) -> None:
        self.count_input()
        self.invalid.append(path)

    def log_issues(self) -> None:
        '''Log any warnings and errors'''
        super().log_issues()
        n_invalid = len(self.invalid)
        if n_invalid:
            log.error("Incoming data issues: {n_invalid} invalid files")

    def check_errors(self) -> None:
        if self.n_errors:
            raise LocalIncomingDataError(self.inconsistent,
                                         self.duplicate,
                                         self.invalid)

    def clear(self) -> None:
        super().clear()
        self.invalid = []


_read_f = partial(pydicom.dcmread, force=True, defer_size=64)


def is_valid_dicom(ds: Dataset) -> bool:
    for uid_elem in uid_elems.values():
        if not hasattr(ds, uid_elem):
            return False
    return True


class LocalChunk(DataChunk):
    '''Mostly naive data chunk corresponding to list of local files
    '''

    report: LocalIncomingReport

    def __init__(self, files: Iterable[PathInputType], description: Optional[str] = None):
        self.files = tuple(files)
        self.description = description
        if description is None:
            rep_descr = str(os.path.dirname(self.files[0])) + ' ...'
        else:
            rep_descr = description + '-incoming'
        self.report = LocalIncomingReport(description=rep_descr, n_expected=self.n_expected)

    @property
    def n_expected(self) -> Optional[int]:
        return len(self.files)

    def __repr__(self) -> str:
        return f'LocalDataChunk([{self.files[0]!r},...,{self.files[-1]!r}])'

    async def gen_data(self) -> AsyncIterator[Dataset]:
        async for _, ds in self.gen_paths_and_data():
            yield ds

    async def gen_paths_and_data(self) -> AsyncIterator[Tuple[PathInputType, Dataset]]:
        '''Generate both the paths and the corresponding data sets'''
        loop = asyncio.get_running_loop()
        for f in self.files:
            ds = await loop.run_in_executor(None, _read_f, os.fspath(f))
            if not is_valid_dicom(ds):
                log.warn("Skipping invalid dicom file: %s", f)
                self.report.add_invalid(f)
                continue
            if not self.report.add(ds):
                continue
            yield f, ds


T_chunk = TypeVar('T_chunk', bound=DataChunk, covariant=True)
T_qreport = TypeVar('T_qreport', bound=Union[Report, MultiReport[Any]], contravariant=True)
T_rreport = TypeVar('T_rreport', bound=Report, contravariant=True)
T_sreport = TypeVar('T_sreport', bound=Union[Report, MultiReport[Any]], covariant=True)
T_oob_chunk = TypeVar('T_oob_chunk', bound=DataChunk, contravariant=True)
T_oob_report = TypeVar('T_oob_report', bound=Union[Report, MultiReport[Any]])


class DataBucket(Generic[T_chunk, T_sreport], Protocol):
    '''Protocol for most basic data stores

    Can just produce one or more DataChunk instances with the
    `gen_data` method, or store data sets through the `send` method
    '''

    description: Optional[str] = None

    _supported_methods: Tuple[TransferMethod, ...] = (TransferMethod.PROXY,)

    async def gen_chunks(self) -> AsyncIterator[T_chunk]:
        '''Generate the data in this bucket, one chunk at a time'''
        raise NotImplementedError
        yield

    @asynccontextmanager
    async def send(self,
                   report: Optional[T_sreport] = None
                   ) -> AsyncIterator['janus._AsyncQueueProxy[Dataset]']:
        '''Produces a Queue that you can put data sets into for storage'''
        raise NotImplementedError
        yield

    def get_empty_send_report(self) -> T_sreport:
        raise NotImplementedError


@runtime_checkable
class DataRepo(Generic[T_chunk, T_qreport, T_sreport, T_rreport], DataBucket[T_chunk, T_sreport], Protocol):
    '''Protocol for stores with query/retrieve functionality
    '''
    async def queries(self,
                      level: Optional[QueryLevel] = None,
                      query: Optional[Dataset] = None,
                      query_res: Optional[QueryResult] = None,
                      report: Optional[T_qreport] = None,
                     ) -> AsyncIterator[QueryResult]:
        '''Returns async generator that produces partial QueryResult objects'''
        raise NotImplementedError
        yield

    async def query(self,
                    level: Optional[QueryLevel] = None,
                    query: Optional[Dataset] = None,
                    query_res: Optional[QueryResult] = None,
                    report: Optional[T_qreport] = None) -> QueryResult:
        '''Perform a query against the data repo'''
        raise NotImplementedError

    def retrieve(self,
                 query_res: QueryResult,
                 report: Optional[T_rreport] = None) -> AsyncIterator[Dataset]:
       '''Returns an async generator that will produce datasets'''
       raise NotImplementedError

    async def gen_query_chunks(self,
                               query_res: QueryResult
                              ) -> AsyncIterator[T_chunk]:
        raise NotImplementedError
        yield

class OobCapable(Generic[T_oob_chunk, T_oob_report], Protocol):
    '''Protocol for stores that are capable of doing out-of-band transfers'''

    async def oob_transfer(self,
                           method: TransferMethod,
                           chunk: T_oob_chunk,
                           report: T_oob_report = None
                          ) -> None:
        '''Perform out-of-band transfer instead of proxying data'''
        raise NotImplementedError

    def get_empty_oob_report(self) -> T_oob_report:
        raise NotImplementedError


class DcmRepo(DataRepo[DcmNetChunk, MultiListReport[DicomOpReport], DicomOpReport, RetrieveReport], OobCapable[DcmNetChunk, MultiListReport[DicomOpReport]], Protocol):
    '''Abstract base class for repos that are DICOM network nodes'''

    _supported_methods: Tuple[TransferMethod, ...] = \
        (TransferMethod.PROXY,
         TransferMethod.REMOTE_COPY)

    @property
    def description(self) -> Optional[str]:
        return str(self.remote.ae_title)

    @property
    def remote(self) -> DcmNode:
        raise NotImplementedError

    @asynccontextmanager
    async def send(self,
                   report: Optional[DicomOpReport] = None
                   ) -> AsyncIterator['janus._AsyncQueueProxy[Dataset]']:
        '''Produces a Queue that you can put data sets into for storage'''
        raise NotImplementedError
        yield

    def get_empty_send_report(self) -> DicomOpReport:
        return DicomOpReport(description=f'-> {self.description}')

    def get_empty_oob_report(self) -> MultiListReport[DicomOpReport]:
        return MultiListReport(description=f'-> {self.description}')


class LocalWriteError(Exception):
    def __init__(self, write_errors: Dict[Exception, List[PathInputType]]):
        self.write_errors = write_errors

    def __str__(self) -> str:
        msg = ['Local write error:']
        for exc, paths in self.write_errors.items():
            msg.append("%d %s errors," % (len(paths), type(exc)))
        return ' '.join(msg)


class LocalWriteReport(Report):

    def __init__(self,
                 description: Optional[str] = None, 
                 n_expected: Optional[int] = None,
                 prog_hook: Optional[ProgressHookBase[Any]] = None,
                ):
        super().__init__(description, n_expected, prog_hook)
        self.write_errors: Dict[Exception, List[PathInputType]] = {}
        self.successful: List[PathInputType] = []
        self.skipped: List[PathInputType] = []

    @property
    def n_success(self) -> int:
        return len(self.successful)

    @property
    def n_errors(self) -> int:
        return len(self.write_errors)

    @property
    def n_warnings(self) -> int:
        return len(self.skipped)

    def add_success(self, path: PathInputType) -> None:
        self.count_input()
        self.successful.append(path)

    def add_error(self, path: PathInputType, exception: Exception) -> None:
        self.count_input()
        if exception not in self.write_errors:
            self.write_errors[exception] = []
        self.write_errors[exception].append(path)

    def add_skipped(self, path: PathInputType) -> None:
        self.count_input()
        self.skipped.append(path)

    def log_issues(self) -> None:
        '''Log a summary of error/warning statuses'''
        if self.n_warnings != 0:
            log.warning("Skipped %d existing files", len(self.skipped))
        if self.n_errors != 0:
            log.error("There were %d write errors" % self.n_errors)

    def check_errors(self) -> None:
        '''Raise an exception if any errors occured'''
        if self.n_errors != 0:
            raise LocalWriteError(self.write_errors)

    def clear(self) -> None:
        self.successful = []
        self.skipped = []
        self.write_errors = {}


class LocalBucket(DataBucket[LocalChunk, LocalWriteReport], OobCapable[LocalChunk, LocalWriteReport], Protocol):
    '''Abstract base class for buckets with local filesystem storage'''

    _supported_methods: Tuple[TransferMethod, ...] = \
        (TransferMethod.PROXY,
         TransferMethod.LINK,
         TransferMethod.SYMLINK,
         TransferMethod.MOVE)

    @asynccontextmanager
    async def send(self,
                   report: Optional[LocalWriteReport] = None
                   ) -> AsyncIterator['janus._AsyncQueueProxy[Dataset]']:
        '''Produces a Queue that you can put data sets into for storage'''
        raise NotImplementedError
        yield

    def get_empty_send_report(self) -> LocalWriteReport:
        return LocalWriteReport(description=f'-> {self.description}')

    def get_empty_oob_report(self) -> LocalWriteReport:
        return LocalWriteReport(description=f'-> {self.description}')
