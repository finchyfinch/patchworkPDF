"""
log_config.py — centralised logging setup for PatchworkPDF.

Call setup_logging() once from entry.py before other modules are imported.
All other modules simply use:
    import logging
    logger = logging.getLogger(__name__)
"""

import os
import sys
import logging
import logging.handlers


def setup_logging():
    """Configure the root logger with a rotating file handler.

    File: logfile.log in the application root directory.
    Rotates at 2 MB; keeps 5 backups → 10 MB cap total.
    Returns the resolved log-file path.
    """
    from platform_io import platform_paths
    # dev: climb from <root>/src/backend/bootstrap/log_config.py to <root> so the
    # dev logfile lands at the project root (not buried in the package tree).
    src_dir = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    # Write next to the source ONLY in a real dev checkout (a writable source dir
    # that isn't frozen or sandboxed).  A frozen build, a Flatpak, or any install
    # under a read-only prefix (/app, /usr, /Applications) uses the per-user data
    # folder instead — always writable without elevation.
    if (getattr(sys, 'frozen', False)
            or platform_paths.is_flatpak()
            or not os.access(src_dir, os.W_OK)):
        root_dir = platform_paths.user_data_dir()
    else:
        root_dir = src_dir

    log_path = os.path.join(root_dir, 'logfile.log')

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        mode='a',                      # always append; never truncate on open
        maxBytes=2 * 1024 * 1024,      # rotate at 2 MB
        backupCount=5,                 # keep 5 backups → 10 MB cap total
        encoding='utf-8',
        delay=False,                   # open the file immediately so the handle
    )                                  # is stable before any log calls arrive
    handler.setFormatter(logging.Formatter(
        '%(asctime)s  %(levelname)-8s  [%(module)s]  %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))

    root = logging.getLogger()
    # Guard against double-registration (e.g. if setup_logging is called twice).
    if not any(isinstance(h, logging.handlers.RotatingFileHandler)
               for h in root.handlers):
        root.setLevel(logging.INFO)
        root.addHandler(handler)

    # Silence noisy third-party loggers so they don't pollute our log.
    # werkzeug: access-log lines already embed their own timestamp, and the
    #           "WARNING: development server" / ANSI startup chatter is noise.
    #           Keep ERROR so genuine server errors still surface.
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    # pikepdf and PIL both emit internal debug/info messages we don't need.
    logging.getLogger('pikepdf').setLevel(logging.WARNING)
    logging.getLogger('PIL').setLevel(logging.WARNING)

    return log_path
