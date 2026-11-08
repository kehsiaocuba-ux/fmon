import os
import json
import stat


# Attributes whose change marks a file as "modified". ctime is recorded but
# deliberately excluded: it changes on metadata-only operations on most
# platforms and would produce noisy false positives.
COMPARED_ATTRS = ("size", "mode", "mtime")


class FolderScanner:
    def __init__(self, root_dir, exclude=()):
        """
        root_dir -- folder to scan recursively.
        exclude  -- iterable of file paths to leave out of the snapshot
                    (e.g. a control file stored inside the monitored folder).
        """
        self.root_dir = os.path.abspath(root_dir)
        self._exclude = {os.path.normcase(os.path.abspath(p)) for p in exclude}

    def scan(self):
        """Recursively walk root_dir (including all subfolders) and return a
        snapshot mapping relative file paths to their size/mode/mtime/ctime.

        Raises FileNotFoundError if root_dir is not an existing directory."""
        if not os.path.isdir(self.root_dir):
            raise FileNotFoundError(f"Directory not found: {self.root_dir}")
        snapshot = {}
        for current_path, dirs, files in os.walk(self.root_dir, followlinks=False):
            for file in files:
                full_path = os.path.join(current_path, file)
                if os.path.normcase(full_path) in self._exclude:
                    continue
                try:
                    stats = os.stat(full_path)
                    rel_path = os.path.relpath(full_path, self.root_dir)
                    snapshot[rel_path] = {
                        "size": stats.st_size,
                        "mode": stat.S_IMODE(stats.st_mode),
                        "mtime": stats.st_mtime,
                        "ctime": stats.st_ctime
                    }
                except (PermissionError, FileNotFoundError):
                    continue
        return snapshot

    def save_control_file(self, output_path):
        """Scan and write the snapshot to output_path, creating the parent
        directory if necessary."""
        data = self.scan()
        parent = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(parent, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return output_path

    @classmethod
    def for_target(cls, target):
        """Build a scanner for a config Target, excluding its own control file."""
        return cls(target.path, exclude=[target.control_file])

    @staticmethod
    def load_control_file(control_path):
        with open(control_path, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def compare(baseline, current):
        """Compare two snapshots.

        Returns a dict with:
          added    - sorted list of (path, entry) present only in `current`
          removed  - sorted list of (path, entry) present only in `baseline`
          modified - sorted list of (path, before_entry, after_entry) whose
                     size, permissions or mtime differ

        Attributes missing from the baseline (e.g. 'mode' in control files
        written by older versions) are not compared.
        """
        base_keys = set(baseline)
        cur_keys = set(current)

        added = [(k, current[k]) for k in sorted(cur_keys - base_keys)]
        removed = [(k, baseline[k]) for k in sorted(base_keys - cur_keys)]
        modified = []
        for k in sorted(base_keys & cur_keys):
            before, after = baseline[k], current[k]
            if any(
                attr in before and attr in after and before[attr] != after[attr]
                for attr in COMPARED_ATTRS
            ):
                modified.append((k, before, after))
        return {"added": added, "removed": removed, "modified": modified}

    def check(self, control_path):
        """Recursively scan root_dir and compare it with a saved control file."""
        baseline = self.load_control_file(control_path)
        return self.compare(baseline, self.scan())


def has_changes(diff):
    return any(diff[key] for key in ("added", "removed", "modified"))


def format_mode(mode):
    """Render permission bits as e.g. '0644 (rw-r--r--)'; '?' if unknown."""
    if mode is None:
        return "?"
    return "%04o (%s)" % (mode, stat.filemode(mode)[1:])
