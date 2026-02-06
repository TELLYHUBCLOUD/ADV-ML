from bot import LOGGER
from bot.helper.ext_utils.status_utils import (
    get_readable_file_size,
    get_readable_time,
    MirrorStatus,
    EngineStatus,
)


class MergeStatus:
    def __init__(self, name, size, gid, obj, listener):
        self._name = name
        self._size = size
        self._gid = gid
        self._obj = obj
        self.listener = listener
        self.engine = EngineStatus().STATUS_MERGE

    def processed_bytes(self):
        return get_readable_file_size(self._obj.processed_bytes)

    def gid(self):
        return self._gid

    def progress(self):
        try:
            return f'{round((self._obj.processed_bytes / self._size) * 100, 2)}%'
        except:
            return '0%'

    def speed(self):
        return f'{get_readable_file_size(self._obj.speed)}/s'

    def name(self):
        return self._name

    def size(self):
        return get_readable_file_size(self._size)

    def eta(self):
        try:
            seconds = (self._size - self._obj.processed_bytes) / self._obj.speed
            return get_readable_time(seconds)
        except:
            return '~'

    def status(self):
        return MirrorStatus.STATUS_MERGING
           
    def task(self):
        return self

    async def cancel_download(self):
        LOGGER.info(f'Cancelling merge: {self._name}')
        if self.listener.subproc:
            self.listener.subproc.kill()
        else:
            self.listener.subproc = 'cancelled'
        await self.listener.on_upload_error('Merge stopped by user!')
