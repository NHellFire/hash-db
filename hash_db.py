#!/usr/bin/env python3
from argparse import ArgumentParser
import hashlib
import json
from mmap import mmap, ACCESS_READ
from os import lstat, readlink, stat_result, walk
from os.path import abspath, dirname, isfile, islink, join as ospj, normpath, relpath
from stat import S_ISLNK, S_ISREG
from sys import stderr

HASH_FILENAME = 'SHA512SUM'
DB_FILENAME = 'hash_db.json'
HASH_FUNCTION = hashlib.sha512
# Mostly used for importing from saved hash files
EMPTY_FILE_HASH = ('cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce'
                   '47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e')

ADDED_COLOR = '\033[01;32m'
REMOVED_COLOR = '\033[01;34m'
MODIFIED_COLOR = '\033[01;31m'
NO_COLOR = '\033[00m'

# 1: 'version' field added
# 2: entry 'type' field added; symlinks now treated correctly
DATABASE_VERSION = 2

def read_hash_output(line):
    pieces = line.strip().split('  ', 1)
    return normpath(pieces[1]), pieces[0]

def read_saved_hashes(hash_file, encoding):
    hashes = {}
    with open(hash_file, 'rb') as f:
        for line in f:
            try:
                filename, file_hash = read_hash_output(line.decode(encoding))
                hashes[filename] = file_hash
            except UnicodeDecodeError as e:
                print("Couldn't decode {!r}: ".format(line), end='')
                print(e)
    return hashes

def find_hash_db_r(path):
    """
    Searches the given path and all of its parent
    directories to find a filename matching DB_FILENAME
    """
    abs_path = abspath(path)
    cur_path = ospj(abs_path, DB_FILENAME)
    if isfile(cur_path):
        return cur_path
    parent = dirname(abs_path)
    if parent != abs_path:
        return find_hash_db_r(parent)

def find_hash_db(path):
    filename = find_hash_db_r(path)
    if filename is None:
        message = "Couldn't find '{}' in '{}' or any parent directories"
        raise FileNotFoundError(message.format(DB_FILENAME, path))
    return filename

class HashEntry:
    TYPE_FILE = 0
    TYPE_SYMLINK = 1

    def __init__(self, filename, size=None, mtime=None, hash=None, type=None):
        # In memory, "filename" should be an absolute path
        self.filename = filename
        self.size = size
        self.mtime = mtime
        self.hash = hash
        self.type = type

    def hash_file(self):
        if isfile(self.filename):
            if lstat(self.filename).st_size > 0:
                with open(self.filename, 'rb') as f:
                    with mmap(f.fileno(), 0, access=ACCESS_READ) as m:
                        return HASH_FUNCTION(m).hexdigest()
            else:
                return EMPTY_FILE_HASH
        elif islink(self.filename):
            # The link target will suffice as the "contents"
            target = readlink(self.filename)
            return HASH_FUNCTION(target.encode()).hexdigest()

    def exists(self):
        return isfile(self.filename) or islink(self.filename)

    def verify(self):
        return self.hash_file() == self.hash

    def update_attrs(self):
        s = lstat(self.filename)
        self.size, self.mtime = s.st_size, s.st_mtime

    def update_type(self):
        if islink(self.filename):
            self.type = self.TYPE_SYMLINK
        elif isfile(self.filename):
            self.type = self.TYPE_FILE

    def update(self):
        self.update_attrs()
        self.update_type()
        self.hash = self.hash_file()

    def __eq__(self, other):
        if isinstance(other, stat_result):
            return (
                self.size == other.st_size and
                self.mtime == other.st_mtime and
                (
                    (self.type == self.TYPE_FILE and S_ISREG(other.st_mode))or
                    (self.type == self.TYPE_SYMLINK and S_ISLNK(other.st_mode))
                )
            )
        return super().__eq__(other)

    def __hash__(self):
        return hash(self.filename)

def fix_symlinks(db):
    for entry in db.entries.values():
        if entry.type is None:
            entry.update_type()
            if entry.type == HashEntry.TYPE_SYMLINK:
                entry.update()

# Intended usage: at version i, you need to run all
# upgrade functions in range(i, DATABASE_VERSION)
db_upgrades = [
    None,
    fix_symlinks,
]

class HashDatabase:
    def __init__(self, path):
        try:
            self.path = dirname(find_hash_db(path))
        except FileNotFoundError:
            self.path = path
        self.entries = {}
        self.version = DATABASE_VERSION

    def save(self):
        filename = ospj(self.path, DB_FILENAME)
        data = {
            'version': self.version,
            'files': {
                relpath(entry.filename, self.path): {
                    'size': entry.size,
                    'mtime': entry.mtime,
                    'hash': entry.hash,
                    'type': entry.type,
                }
                for entry in self.entries.values()
            }
        }
        with open(filename, 'w') as f:
            json.dump(data, f)

    def load(self):
        filename = find_hash_db(self.path)
        with open(filename) as f:
            data = json.load(f)
        self.version = data['version']
        for filename, entry_data in data['files'].items():
            entry = HashEntry(abspath(ospj(self.path, filename)))
            entry.size = entry_data.get('size')
            entry.mtime = entry_data.get('mtime')
            entry.hash = entry_data.get('hash')
            entry.type = entry_data.get('type')
            self.entries[entry.filename] = entry
        for i in range(self.version, DATABASE_VERSION):
            db_upgrades[i](self)
        self.version = DATABASE_VERSION

    def import_hashes(self, filename, encoding):
        """
        Imports a hash file created by e.g. sha512sum, and populates
        the database with this data. Examines each file to obtain the
        size and mtime information.

        Returns the number of file hashes imported.
        """
        hashes = read_saved_hashes(filename, encoding)
        for filename, hash in hashes.items():
            entry = HashEntry(abspath(ospj(self.path, filename.replace('\\\\', '\\'))))
            entry.hash = hash
            entry.update_attrs()
            self.entries[entry.filename] = entry
        return len(self.entries)

    def _find_changes(self):
        """
        Walks the filesystem. Identifies noteworthy files -- those
        that were added, removed, or changed (size, mtime or type).

        Returns a 3-tuple of sets of HashEntry objects:
        [0] added files
        [1] removed files
        [2] modified files

        self.entries is not modified; this method only reports changes.
        """
        added = set()
        modified = set()
        existing_files = set()
        for dirpath, _, filenames in walk(self.path):
            for filename in filenames:
                if filename == DB_FILENAME:
                    continue
                abs_filename = abspath(ospj(dirpath, filename))
                if abs_filename in self.entries:
                    entry = self.entries[abs_filename]
                    existing_files.add(entry)
                    st = lstat(abs_filename)
                    if entry != st:
                        modified.add(entry)
                else:
                    entry = HashEntry(abs_filename)
                    entry.update_attrs()
                    added.add(entry)
        removed = set(self.entries.values()) - existing_files
        return added, removed, modified

    def update(self):
        """
        Walks the filesystem, adding and removing files from
        the database as appropriate.

        Returns a 3-tuple of sets of filenames:
        [0] added files
        [1] removed files
        [2] modified files
        """
        added, removed, modified = self._find_changes()
        for entry in added:
            entry.update()
            self.entries[entry.filename] = entry
        for entry in removed:
            del self.entries[entry.filename]
        # Entries will appear in 'modified' if the size, mtime or type
        # change. I've seen a lot of spurious mtime mismatches on vfat
        # filesystems (like on USB flash drives), so only report files
        # as modified if the hash changes.
        content_modified = set()
        for entry in modified:
            old_hash = entry.hash
            entry.update()
            if entry.hash != old_hash:
                content_modified.add(entry)
        return (
            {entry.filename for entry in added},
            {entry.filename for entry in removed},
            {entry.filename for entry in content_modified}
        )

    def status(self):
        added, removed, modified = self._find_changes()
        return (
            {entry.filename for entry in added},
            {entry.filename for entry in removed},
            {entry.filename for entry in modified}
        )

    def verify(self, verbose_failures=False):
        """
        Calls each HashEntry's verify method to make sure that
        nothing has changed on disk.

        Returns a 2-tuple of sets of filenames:
        [0] modified files
        [1] removed files
        """
        modified = set()
        removed = set()
        count = len(self.entries)
        # TODO: Track number of bytes hashed instead of number of files
        # This will act as a more meaningful progress indicator
        i = -1
        for i, entry in enumerate(self.entries.values()):
            if entry.exists():
                if not entry.verify():
                    if verbose_failures:
                        stderr.write('\r{} failed hash verification\n'.format(entry.filename))
                    modified.add(entry.filename)
            else:
                removed.add(entry.filename)
                if verbose_failures:
                    stderr.write('\r{} is missing\n'.format(entry.filename))
            stderr.write('\rChecked {} of {} files'.format(i + 1, count))
        if i >= 0:
            stderr.write('\n')
        return modified, removed

def print_file_lists(added, removed, modified):
    if added:
        print(ADDED_COLOR + 'Added files:' + NO_COLOR)
        for filename in sorted(added):
            print(filename)
        print()
    if removed:
        print(REMOVED_COLOR + 'Removed files:' + NO_COLOR)
        for filename in sorted(removed):
            print(filename)
        print()
    if modified:
        print(MODIFIED_COLOR + 'Modified files:' + NO_COLOR)
        for filename in sorted(modified):
            print(filename)
        print()

def init(db, pretend):
    print('Initializing hash database')
    added, removed, modified = db.update()
    print_file_lists(added, removed, modified)
    if not pretend:
        db.save()

def update(db, pretend):
    print('Updating hash database')
    db.load()
    added, removed, modified = db.update()
    print_file_lists(added, removed, modified)
    if not pretend:
        db.save()

def status(db, pretend):
    db.load()
    added, removed, modified = db.status()
    print_file_lists(added, removed, modified)

def import_hashes(db, pretend):
    print('Importing hash database')
    count = db.import_hashes(ospj(args.directory, HASH_FILENAME),
                             encoding=args.import_encoding)
    print('Imported {} entries'.format(count))
    if not args.pretend:
        db.save()

def verify(db, pretend):
    db.load()
    modified, removed = db.verify(args.verbose_failures)
    print_file_lists(None, removed, modified)

functions = {
    'init': init,
    'update': update,
    'status': status,
    'import': import_hashes,
    'verify': verify,
}

if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument('command', choices=sorted(functions.keys()))
    parser.add_argument('directory', default='.', nargs='?')
    parser.add_argument('-n', '--pretend', action='store_true')
    parser.add_argument('--import-encoding', default='utf-8', help=('Encoding of the '
        'file used for import. Default: utf-8.'))
    parser.add_argument('--verbose-failures', action='store_true', help=('If hash '
        'verification fails, print filenames as soon as they are known in addition '
        'to the post-hashing summary.'))
    args = parser.parse_args()
    db = HashDatabase(args.directory)
    functions[args.command](db, args.pretend)
