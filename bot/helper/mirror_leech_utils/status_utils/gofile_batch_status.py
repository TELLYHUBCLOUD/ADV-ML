from ...ext_utils.status_utils import (
    MirrorStatus,
    get_readable_file_size,
    get_readable_time,
)


class GofileBatchStatus:
    def __init__(self, listener, batch_uploader, gid):
        self.listener = listener
        self._batch_uploader = batch_uploader
        self._gid = gid
        self.engine = "Gofile Batch"

    def processed_bytes(self):
        # Show current file's uploaded bytes
        if self._batch_uploader.current_uploader:
            return get_readable_file_size(self._batch_uploader.current_uploader.processed_bytes)
        return get_readable_file_size(0)

    def size(self):
        return get_readable_file_size(self.listener.size)

    def status(self):
        return MirrorStatus.STATUS_GOFILE  # Shows "GofileUp"

    def name(self):
        return self.listener.name

    def progress(self):
        # Calculate real-time progress based on current file upload
        try:
            if self._batch_uploader.total_files > 0:
                # Completed files
                completed = self._batch_uploader.uploaded_files - 1
                # Current file upload progress (0-1)
                if self._batch_uploader.current_uploader and self.listener.subsize > 0:
                    current_progress = self._batch_uploader.current_uploader.processed_bytes / self.listener.subsize
                else:
                    current_progress = 0
                # Total progress = (completed files + current file progress) / total files
                progress_raw = ((completed + current_progress) / self._batch_uploader.total_files) * 100
            else:
                progress_raw = 0
        except:
            progress_raw = 0
        return f"{round(progress_raw, 2)}%"

    def speed(self):
        # Get speed from current file uploader
        if self._batch_uploader.current_uploader:
            speed_bytes = self._batch_uploader.current_uploader.speed
            return f"{get_readable_file_size(speed_bytes)}/s"
        return "0B/s"

    def eta(self):
        return "-"

    def gid(self):
        return self._gid

    def task(self):
        return self._batch_uploader
