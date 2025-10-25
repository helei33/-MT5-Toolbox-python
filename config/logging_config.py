import logging
import sys
from logging.handlers import RotatingFileHandler
from queue import Queue
import os

class QueueHandler(logging.Handler):
    """
    A custom logging handler that puts log records into a queue.
    """
    def __init__(self, queue):
        super().__init__()
        self.queue = queue

    def emit(self, record):
        # Put the raw record into the queue for the UI to format
        self.queue.put(record)

def setup_logging(gui_queue: Queue):
    """
    Sets up the root logger for the entire application.
    It now accepts a queue from the UI.
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

    # 3. QueueHandler to send logs to the GUI (using the provided queue)
    queue_handler = QueueHandler(gui_queue)
    queue_handler.setLevel(logging.INFO)

    # --- Create formatter and add it to handlers ---
    # Formatter for console and file
    detailed_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    stream_handler.setFormatter(detailed_formatter)
    file_handler.setFormatter(detailed_formatter)

    # The GUI will format its own messages, so the queue_handler doesn't need a formatter.

    # --- Add handlers to the logger ---
    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    logger.addHandler(queue_handler)

    return logger
