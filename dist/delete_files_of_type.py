import os
import sys

def delete_files_by_extension(root_dir, extension):
    """
    Recursively find and delete files with the given extension in root_dir.
    :param root_dir: Directory to search in.
    :param extension: File extension to match (e.g., '.txt').
    """
    if not os.path.isdir(root_dir):
        print(f"Error: '{root_dir}' is not a valid directory.")
        return

    if not extension.startswith('.'):
        extension = '.' + extension  # Ensure extension starts with '.'

    deleted_count = 0

    for dirpath, _, filenames in os.walk(root_dir):
        for filename in filenames:
            if filename.lower().endswith(extension.lower()):
                file_path = os.path.join(dirpath, filename)
                try:
                    os.remove(file_path)
                    print(f"Deleted: {file_path}")
                    deleted_count += 1
                except PermissionError:
                    print(f"Permission denied: {file_path}")
                except OSError as e:
                    print(f"Error deleting {file_path}: {e}")

    print(f"\nTotal files deleted: {deleted_count}")


if __name__ == "__main__":
    # Ensure correct usage
    if len(sys.argv) != 3:
        print("Usage: python delete_files.py <directory> <extension>")
        print("Example: python delete_files.py /path/to/dir .log")
        sys.exit(1)

    target_dir = sys.argv[1]
    file_ext = sys.argv[2]

    # Safety confirmation
    print(f"WARNING: This will permanently delete all '{file_ext}' files in '{target_dir}' and subdirectories.")
    confirm = input("Type 'yes' to continue: ").strip().lower()
    if confirm == 'yes':
        delete_files_by_extension(target_dir, file_ext)
    else:
        print("Operation cancelled.")
