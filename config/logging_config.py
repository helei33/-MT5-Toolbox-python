import logging
import sys
from logging.handlers import RotatingFileHandler
from queue import Queue
import os

# The UI will listen to this queue for log messages
log_queue = Queue()

class QueueHandler(logging.Handler):
    """
    A custom logging handler that puts log records into a queue.
    """
    def __init__(self, queue):
        super().__init__()
        self.queue = queue

    def emit(self, record):
        # Only put the formatted message into the queue
        self.queue.put(self.format(record))

def setup_logging():
    """
    Sets up the root logger for the entire application.
    """
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Avoid adding handlers if they already exist
    if logger.hasHandlers():
        logger.handlers.clear()

    # --- Create handlers ---
    # 1. StreamHandler to print to console
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)

    # 2. RotatingFileHandler to save to a file
    logs_dir = 'logs'
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir)
    file_handler = RotatingFileHandler(os.path.join(logs_dir, 'app.log'), maxBytes=1024*1024*5, backupCount=2, encoding='utf-8')
    file_handler.setLevel(logging.INFO)

    # 3. QueueHandler to send logs to the GUI
    queue_handler = QueueHandler(log_queue)
    queue_handler.setLevel(logging.INFO)

    # --- Create formatter and add it to handlers ---
    # Formatter for console and file
    detailed_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    stream_handler.setFormatter(detailed_formatter)
    file_handler.setFormatter(detailed_formatter)

    # Formatter for the GUI queue
    gui_formatter = logging.Formatter('%(asctime)s - %(message)s')
    queue_handler.setFormatter(gui_formatter)

    # --- Add handlers to the logger ---
    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    logger.addHandler(queue_handler)

    return logger, log_queue

# --- Create and export a global logger instance and the queue for the GUI ---
logger, gui_log_queue = setup_logging()
