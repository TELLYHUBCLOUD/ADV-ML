import asyncio
from logging import getLogger
from os import path as ospath, walk
from re import sub as re_sub
from aiofiles.os import path as aiopath
from natsort import natsorted

from ....core.config_manager import Config
from .... import task_dict, task_dict_lock
from ...ext_utils.bot_utils import sync_to_async
from ...telegram_helper.message_utils import update_status_message
from ..uphoster_utils.gofile_utils.upload import GoFileUpload
from ..status_utils.gofile_batch_status import GofileBatchStatus

LOGGER = getLogger(__name__)


class GofileBatchUploader:
    def __init__(self, listener, tg_uploader=None):
        self.listener = listener
        self.gofile_links = {}  # filename -> gofile_link mapping
        self.total_files = 0
        self.uploaded_files = 0
        self.current_file = ""
        self.current_uploader = None  # Track current file's uploader for speed
        
        # Check if gofile is enabled
        self._gofile_leech_enabled = listener.user_dict.get("GOFILE_LEECH_ENABLED", False)
        self._gofile_token = (
            listener.user_dict.get("GOFILE_TOKEN") or 
            getattr(Config, "GOFILE_API", None)
        )
        self.enabled = bool(self._gofile_leech_enabled and self._gofile_token)

    async def upload_all_files(self, path):
        """Upload all files in the path to gofile and return mapping of filename -> link"""
        if not self.enabled:
            return {}
        
        # Validate token once at the start to avoid redundant checks for each file
        try:
            if not await GoFileUpload.is_goapi(self._gofile_token):
                LOGGER.error("Invalid GoFile API token!")
                return {}
        except Exception as e:
            LOGGER.error(f"Failed to validate GoFile API token: {e}")
            return {}
        
        # Get list of files to upload
        file_list = []
        for dirpath, _, files in await sync_to_async(walk, path):
            for file_ in natsorted(files):
                file_path = ospath.join(dirpath, file_)
                if await aiopath.isfile(file_path):
                    file_list.append((file_path, file_))
        
        if not file_list:
            return {}
        
        self.total_files = len(file_list)
        
        # Set up files_to_proceed for count display
        self.listener.files_to_proceed = [f"File {i+1}" for i in range(self.total_files)]
        self.listener.proceed_count = 0
        
        try:
            for file_path, file_name in file_list:
                if self.listener.is_cancelled:
                    break
                
                self.uploaded_files += 1
                self.listener.proceed_count = self.uploaded_files
                
                # Use filename as-is from disk (already has nameswap + prefix/suffix applied)
                self.current_file = file_name
                
                # Update batch progress in main listener
                self.listener.subname = f"Uploading to Gofile... ({self.uploaded_files}/{self.total_files}) • {file_name}"
                self.listener.subsize = await aiopath.getsize(file_path)
                
                # Update status message to show batch progress
                await update_status_message(self.listener.message.chat.id)
                
                # Upload single file with filename from disk
                gofile_link = await self._upload_single_file(file_path, file_name)
                if gofile_link:
                    # Store with disk filename for matching with Telegram upload
                    self.gofile_links[file_name] = gofile_link
                else:
                    LOGGER.warning(f"Failed to upload {file_name} to gofile")
        
        finally:
            # Clean up batch operation
            try:
                # Clear sub operation info
                self.listener.subname = ""
                self.listener.subsize = 0
                self.listener.proceed_count = 0
                self.listener.files_to_proceed = []
                
                # Update status message
                await update_status_message(self.listener.message.chat.id)
            except:
                pass
        
        return self.gofile_links

    async def _upload_single_file(self, file_path, file_name):
        """Upload a single file to gofile with retry logic"""
        max_retries = 5
        retry_delay = 3  # seconds between retries
        
        for attempt in range(1, max_retries + 1):
            try:
                # Result holder
                result_link = None
                upload_error = None
                
                # Simple listener for GoFileUpload
                class SimpleGofileListener:
                    def __init__(self, user_dict, filename, user_id, file_size, original_listener):
                        self.user_dict = user_dict
                        self.user_id = user_id
                        self.name = filename
                        self.is_cancelled = False
                        self.size = file_size
                        
                        # Required attributes for status system
                        self.subname = ""
                        self.subsize = 0
                        self.proceed_count = 0
                        self.files_to_proceed = []
                        self.progress = True
                        
                        # Copy from original listener
                        self.message = original_listener.message
                        self.is_super_chat = original_listener.is_super_chat
                        self.mode = original_listener.mode
                    
                    async def on_upload_error(self, error):
                        nonlocal upload_error
                        upload_error = error
                    
                    async def on_upload_complete(self, link, files, folders, mime_type, dir_id=""):
                        nonlocal result_link
                        result_link = link
                
                # Get file size
                file_size = await aiopath.getsize(file_path)
                listener = SimpleGofileListener(self.listener.user_dict, file_name, self.listener.user_id, file_size, self.listener)
                
                # Upload directly from disk (filename already has all transformations)
                # Skip token validation since we already validated it at the start of batch upload
                uploader = GoFileUpload(listener, file_path, skip_token_validation=True)
                self.current_uploader = uploader  # Store for speed tracking
                await uploader.upload()
                
                if upload_error:
                    raise Exception(upload_error)
                
                if result_link:
                    if attempt > 1:
                        LOGGER.info(f"Gofile upload succeeded for {file_name} on attempt {attempt}")
                    return result_link
                else:
                    raise Exception("Upload completed but no link returned")
                
            except Exception as e:
                if attempt < max_retries:
                    LOGGER.warning(f"Gofile upload failed for {file_name} (attempt {attempt}/{max_retries}): {str(e)}. Retrying in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                else:
                    LOGGER.error(f"Gofile upload failed for {file_name} after {max_retries} attempts: {str(e)}")
                    return None

    def get_gofile_link(self, filename):
        """Get gofile link for a specific filename"""
        return self.gofile_links.get(filename, None)

    async def cancel_task(self):
        """Cancel the current gofile batch upload operation"""
        self.listener.is_cancelled = True
        if self.current_uploader:
            # Cancel the inner uploader's listener so upload_file() checks work
            self.current_uploader.listener.is_cancelled = True
            await self.current_uploader.cancel_task()
        LOGGER.info(f"Cancelling GoFile batch upload for: {self.listener.name}")