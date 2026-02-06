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
from ..uphoster_utils.devuploads_utils.upload import DevUploadsUpload
from ..status_utils.devuploads_batch_status import DevUploadsBatchStatus

LOGGER = getLogger(__name__)


class DevUploadsBatchUploader:
    def __init__(self, listener, tg_uploader=None):
        self.listener = listener
        self.devuploads_links = {}  # filename -> devuploads_link mapping
        self.total_files = 0
        self.uploaded_files = 0
        self.current_file = ""
        self.current_uploader = None  # Track current file's uploader for speed
        self._sess_id = None  # Cached session ID
        self._server_url = None  # Cached server URL
        
        # Check if devuploads is enabled
        self._devuploads_leech_enabled = listener.user_dict.get("DEVUPLOADS_LEECH_ENABLED", False)
        self._devuploads_api_key = (
            listener.user_dict.get("DEVUPLOADS_API_KEY") or 
            getattr(Config, "DEVUPLOADS_API_KEY", None)
        )
        self.enabled = bool(self._devuploads_leech_enabled and self._devuploads_api_key)

    async def upload_all_files(self, path):
        """Upload all files in the path to devuploads and return mapping of filename -> link"""
        if not self.enabled:
            return {}
        
        # Validate API key and get upload server once at the start
        try:
            # Create a temporary uploader to get server credentials
            temp_uploader = DevUploadsUpload(self.listener, path, skip_token_validation=False)
            if not await temp_uploader.get_upload_server():
                LOGGER.error("Invalid DevUploads API key or failed to get upload server!")
                return {}
            
            # Store the session credentials for reuse
            self._sess_id = temp_uploader._sess_id
            self._server_url = temp_uploader._server_url
        except Exception as e:
            LOGGER.error(f"Failed to validate DevUploads API key: {e}")
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
                self.listener.subname = f"Uploading to DevUploads... ({self.uploaded_files}/{self.total_files}) • {file_name}"
                self.listener.subsize = await aiopath.getsize(file_path)
                
                # Update status message to show batch progress
                await update_status_message(self.listener.message.chat.id)
                
                # Upload single file with filename from disk
                devuploads_link = await self._upload_single_file(file_path, file_name)
                if devuploads_link:
                    # Store with disk filename for matching with Telegram upload
                    self.devuploads_links[file_name] = devuploads_link
                else:
                    LOGGER.warning(f"Failed to upload {file_name} to devuploads")
        
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
        
        return self.devuploads_links

    async def _upload_single_file(self, file_path, file_name):
        """Upload a single file to devuploads with retry logic"""
        max_retries = 5
        retry_delay = 3  # seconds between retries
        
        for attempt in range(1, max_retries + 1):
            try:
                # Result holder
                result_link = None
                upload_error = None
                
                # Simple listener for DevUploadsUpload
                class SimpleDevUploadsListener:
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
                listener = SimpleDevUploadsListener(self.listener.user_dict, file_name, self.listener.user_id, file_size, self.listener)
                
                # Upload directly from disk (filename already has all transformations)
                # Reuse session credentials from batch uploader
                uploader = DevUploadsUpload(listener, file_path, skip_token_validation=True)
                uploader._sess_id = self._sess_id  # Reuse session ID
                uploader._server_url = self._server_url  # Reuse server URL
                self.current_uploader = uploader  # Store for speed tracking
                await uploader.upload()
                
                if upload_error:
                    raise Exception(upload_error)
                
                if result_link:
                    if attempt > 1:
                        LOGGER.info(f"DevUploads upload succeeded for {file_name} on attempt {attempt}")
                    return result_link
                else:
                    raise Exception("Upload completed but no link returned")
                
            except Exception as e:
                if attempt < max_retries:
                    LOGGER.warning(f"DevUploads upload failed for {file_name} (attempt {attempt}/{max_retries}): {str(e)}. Retrying in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                else:
                    LOGGER.error(f"DevUploads upload failed for {file_name} after {max_retries} attempts: {str(e)}")
                    return None

    def get_devuploads_link(self, filename):
        """Get devuploads link for a specific filename"""
        return self.devuploads_links.get(filename, None)

    async def cancel_task(self):
        """Cancel the current devuploads batch upload operation"""
        self.listener.is_cancelled = True
        if self.current_uploader:
            # Cancel the inner uploader's listener so upload_file() checks work
            self.current_uploader.listener.is_cancelled = True
            await self.current_uploader.cancel_task()
        LOGGER.info(f"Cancelling DevUploads batch upload for: {self.listener.name}")
