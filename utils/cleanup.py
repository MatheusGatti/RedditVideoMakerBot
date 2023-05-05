import os
from os.path import exists
import shutil


def _listdir(d):  # listdir with full path
    return [os.path.join(d, f) for f in os.listdir(d)]


def cleanup(id) -> int:
    """Deletes all temporary assets in assets/temp

    Returns:
        int: How many files were deleted
    """
    path = f"../assets/temp/{id}/"
    if exists(path):
        try:
            shutil.rmtree(path)
            return len(os.listdir(path))
        except Exception as e:
            print(f"Error deleting temporary files: {e}")
            return 0
    return 0
