from io import BufferedReader, IOBase
from logging import getLogger
from os import path as ospath
from os import walk as oswalk
from pathlib import Path
from asyncio import create_task

from aiofiles.os import path as aiopath
from aiohttp import ClientSession, FormData, payload
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bot.core.config_manager import Config
from bot.helper.ext_utils.bot_utils import SetInterval, sync_to_async

LOGGER = getLogger(__name__)


class ProgressFileWrapper(IOBase):
    """File-like wrapper to track upload progress"""

    def __init__(self, file_obj, callback=None):
        self.file_obj = file_obj
        self.callback = callback
        self.total_read = 0

    def read(self, size=-1):
        data = self.file_obj.read(size)
        if data:
            self.total_read += len(data)
            if self.callback:
                self.callback(self.total_read)
        return data

    def seek(self, *args, **kwargs):
        return self.file_obj.seek(*args, **kwargs)

    def tell(self):
        return self.file_obj.tell()

    def close(self):
        return self.file_obj.close()

    @property
    def name(self):
        return getattr(self.file_obj, "name", "")

    def readable(self):
        return True

    def writable(self):
        return False

    def seekable(self):
        return True


class ProgressFileReader(BufferedReader):
    def __init__(self, filename, read_callback=None):
        super().__init__(open(filename, "rb"))
        self.__read_callback = read_callback
        self.length = Path(filename).stat().st_size

    def read(self, size=None):
        size = size or (self.length - self.tell())
        if self.__read_callback:
            self.__read_callback(self.tell())
        return super().read(size)


class DevUploadsUpload:
    def __init__(self, listener, path, skip_token_validation=False):
        self.listener = listener
        self._updater = None
        self._path = path
        self._is_errored = False
        self._upload_task = None
        self.server_api_url = "https://devuploads.com/api/upload/server"
        self.__processed_bytes = 0
        self.last_uploaded = 0
        self.total_time = 0
        self.total_files = 0
        self.total_folders = 0
        self.is_uploading = True
        self.update_interval = 3
        self.skip_token_validation = skip_token_validation
        self._sess_id = None
        self._server_url = None

        # Get user-specific API key
        from bot import user_data

        user_dict = user_data.get(self.listener.user_id, {})
        self.api_key = user_dict.get("DEVUPLOADS_API_KEY") or Config.DEVUPLOADS_API_KEY

    @property
    def speed(self):
        try:
            return self.__processed_bytes / self.total_time
        except Exception:
            return 0

    @property
    def processed_bytes(self):
        return self.__processed_bytes

    def __progress_callback(self, current):
        chunk_size = current - self.last_uploaded
        self.last_uploaded = current
        self.__processed_bytes += chunk_size

    async def progress(self):
        self.total_time += self.update_interval

    async def get_upload_server(self):
        """Get upload server URL and session ID"""
        if not self.api_key:
            raise ValueError("DevUploads API key not found!")

        try:
            async with ClientSession() as session:
                url = f"{self.server_api_url}?key={self.api_key}"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        if result.get("status") == 200:
                            self._sess_id = result.get("sess_id")
                            self._server_url = result.get("result")
                            return True
                        else:
                            LOGGER.error(f"Failed to get upload server: {result}")
                            return False
                    else:
                        LOGGER.error(f"HTTP error {resp.status} getting upload server")
                        return False
        except Exception as e:
            LOGGER.error(f"Error getting upload server: {e}")
            return False

    @staticmethod
    async def is_valid_api_key(api_key):
        """Validate DevUploads API key by getting upload server"""
        if not api_key:
            return False

        try:
            async with ClientSession() as session:
                url = f"https://devuploads.com/api/upload/server?key={api_key}"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        return result.get("status") == 200
                    return False
        except Exception as e:
            LOGGER.error(f"API key validation error: {e}")
            return False

    @retry(
        retry=retry_if_exception_type((Exception,)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=8),
        reraise=True,
    )
    async def upload_file(self, file_path: str):
        """Upload a single file to DevUploads"""
        if not self.api_key:
            raise ValueError("DevUploads API key not found!")

        if self.listener.is_cancelled:
            return None

        # Get upload server if not already fetched
        if not self._sess_id or not self._server_url:
            if not await self.get_upload_server():
                raise Exception("Failed to get upload server credentials")

        file_name = ospath.basename(file_path)
        file_handle = None

        try:
            # Open file
            file_handle = open(file_path, "rb")
            file_size = Path(file_path).stat().st_size

            # Wrap with progress tracker
            progress_file = ProgressFileWrapper(file_handle, self.__progress_callback)

            async with ClientSession() as session:
                data = FormData()
                data.add_field("sess_id", self._sess_id)
                data.add_field("utype", "reg")  # registered user type

                # Add file with progress tracking
                data.add_field(
                    "file",
                    progress_file,
                    filename=file_name,
                )

                async with session.post(
                    self._server_url,
                    data=data,
                    timeout=3600,  # 1 hour timeout for large files
                ) as resp:
                    if resp.status != 200:
                        raise Exception(f"Upload failed with status {resp.status}")

                    result = await resp.json()

                    # Handle both list and dict responses
                    if isinstance(result, list):
                        # If response is a list, check first element
                        if result and isinstance(result[0], dict):
                            file_code = result[0].get("file_code")
                        else:
                            raise Exception(
                                f"Unexpected list response format: {result}"
                            )
                    elif isinstance(result, dict):
                        # If response is a dict, get file_code directly
                        file_code = result.get("file_code")
                    else:
                        raise Exception(f"Unexpected response type: {type(result)}")

                    if file_code:
                        # Return the DevUploads URL
                        return f"https://devuploads.com/{file_code}"
                    else:
                        error_msg = result.get("message", "Unknown error")
                        raise Exception(f"DevUploads upload failed: {error_msg}")
        except Exception as e:
            LOGGER.error(f"Error uploading {file_name}: {str(e)}")
            raise
        finally:
            # Ensure file handle is always closed
            if file_handle:
                file_handle.close()

    async def _upload_dir(self, input_directory):
        """Upload all files in a directory"""
        uploaded_links = []

        for root, _dirs, files in await sync_to_async(oswalk, input_directory):
            if self.listener.is_cancelled:
                break

            for file in files:
                if self.listener.is_cancelled:
                    break

                file_path = ospath.join(root, file)
                try:
                    link = await self.upload_file(file_path)
                    if link:
                        uploaded_links.append(link)
                        self.total_files += 1
                except Exception as e:
                    LOGGER.error(f"Failed to upload {file}: {str(e)}")

        return uploaded_links

    async def upload(self):
        try:
            self._updater = SetInterval(self.update_interval, self.progress)

            if not self.api_key:
                raise ValueError(
                    "DevUploads API key not configured! Please set your DevUploads API key in user settings."
                )

            # Run the async process in a task that can be cancelled
            self._upload_task = create_task(self._upload_process())
            await self._upload_task

        except Exception as err:
            if isinstance(err, RetryError):
                LOGGER.info(f"Total Attempts: {err.last_attempt.attempt_number}")
                err = err.last_attempt.exception()
            err = str(err).replace(">", "").replace("<", "")
            LOGGER.error(err)
            await self.listener.on_upload_error(err)
            self._is_errored = True
        finally:
            if self._updater:
                self._updater.cancel()
            if (
                self.listener.is_cancelled and not self._is_errored
            ) or self._is_errored:
                return

    async def _upload_process(self):
        """Main upload process"""
        # Get upload server credentials (validates API key)
        if not self.skip_token_validation:
            if not await self.get_upload_server():
                raise Exception(
                    "Invalid DevUploads API Key or failed to get upload server!"
                )

        if await aiopath.isfile(self._path):
            # Single file upload
            link = await self.upload_file(self._path)
            if link:
                mime_type = "File"
                self.total_files = 1
            else:
                raise ValueError("Failed to upload file to DevUploads")
        elif await aiopath.isdir(self._path):
            # Directory upload - upload all files and return text with all links
            links = await self._upload_dir(self._path)
            if links:
                link = "\n".join(links)
                mime_type = "Folder"
            else:
                raise ValueError("Failed to upload folder to DevUploads")
        else:
            raise ValueError("Invalid file path!")

        if self.listener.is_cancelled:
            return

        LOGGER.info(f"Uploaded To DevUploads: {self.listener.name}")
        await self.listener.on_upload_complete(
            link,
            self.total_files,
            self.total_folders,
            mime_type,
            dir_id="",
        )

    async def cancel_task(self):
        self.listener.is_cancelled = True
        if self.is_uploading:
            LOGGER.info(f"Cancelling DevUploads Upload: {self.listener.name}")
            if self._upload_task and not self._upload_task.done():
                self._upload_task.cancel()
            await self.listener.on_upload_error("DevUploads upload has been cancelled!")
