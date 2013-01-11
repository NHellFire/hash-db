#!/usr/bin/env python
from argparse import ArgumentParser
import hashlib
import json
from mmap import mmap, ACCESS_READ
from os import stat, walk
from os.path import abspath, dirname, isfile, join as ospj, normpath, relpath

HASH_FILENAME = 'SHA512SUM'
DB_FILENAME = 'hash_db.json'
HASH_FUNCTION = hashlib.sha512
# Mostly used for importing from saved hash files
EMPTY_FILE_HASH = ('cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce'
                   '47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e')

def read_hash_output(line):
    pieces = line.strip().split('  ', 1)
    return normpath(pieces[1]), pieces[0]

def read_saved_hashes(hash_file):
    hashes = {}
    with open(hash_file, 'rb') as f:
        for line in f:
            filename, file_hash = read_hash_output(line)
            hashes[filename] = file_hash
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
        raise FileNotFoundError(message.format(HASH_FILENAME, path))
    return filename

class HashEntry:
    def __init__(self, filename, size=None, mtime=None, hash=None):
        # In memory, "filename" should be an absolute path
        self.filename = filename
        self.size = size
        self.mtime = mtime
        self.hash = hash

    def hash_file(self):
        if stat(self.filename).st_size > 0:
            with open(self.filename, 'rb') as f:
                with mmap(f.fileno(), 0, access=ACCESS_READ) as m:
                    return HASH_FUNCTION(m).hexdigest()
        else:
            return EMPTY_FILE_HASH

    def verify(self):
        return isfile(self.filename) and self.hash_file() == self.hash

    def update_attrs(self):
        s = stat(self.filename)
        self.size, self.mtime = s.st_size, s.st_mtime

    def update(self):
        self.update_attrs()
        self.hash = self.hash_file()

class HashDatabase:
    def __init__(self, path):
        self.path = path
        self.entries = {}

    def save(self):
        filename = ospj(self.path, DB_FILENAME)
        data = {
            relpath(entry.filename, self.path): {
                'size': entry.size,
                'mtime': entry.mtime,
                'hash': entry.hash
            }
            for entry in self.entries.values()
        }
        with open(filename, 'w') as f:
            json.dump(data, f)

    def load(self):
        filename = find_hash_db(self.path)
        with open(filename) as f:
            data = json.load(f)
        for filename, entry_data in data.items():
            entry = HashEntry(abspath(ospj(self.path, filename)))
            entry.size = entry_data['size']
            entry.mtime = entry_data['mtime']
            entry.hash = entry_data['hash']
            self.entries[entry.filename] = entry

    def import_hashes(self, filename):
        """
        Imports a hash file created by e.g. sha512sum, and populates
        the database with this data. Examines each file to obtain the
        size and mtime information.
        """
        hashes = read_saved_hashes(filename)
        for filename, hash in hashes:
            entry = HashEntry(abspath(ospj(self.path, filename)))
            entry.update_attrs()
            self.entries[entry.filename] = entry

    def update(self):
        """
        Walks the filesystem, adding and removing files from
        the database as appropriate.
        """
        existing_files = set()
        for dirpath, _, filenames in walk(self.path):
            for filename in filenames:
                if filename == DB_FILENAME:
                    continue
                abs_filename = abspath(ospj(dirpath, filename))
                existing_files.add(abs_filename)
                if abs_filename in self.entries:
                    entry = self.entries[abs_filename]
                    st = stat(abs_filename)
                    if entry.size != st.st_size or entry.mtime != st.st_mtime:
                        print('{} has changed; updating hash'.format(abs_filename))
                        entry.update()
                    else:
                        pass
                        #print('{} has not changed'.format(abs_filename))
                else:
                    print('Adding new entry for {}'.format(abs_filename))
                    entry = HashEntry(abs_filename)
                    entry.update()
                    self.entries[abs_filename] = entry
        for deleted_file in self.entries.keys() - existing_files:
            print('Removing entry for deleted file {}'.format(deleted_file))
            del self.entries[deleted_file]

    def verify(self):
        """
        Calls each HashEntry's verify method to make sure that
        nothing has changed on disk.

        Yields each filename with different contents than was
        recorded here.
        """
        for filename, entry in self.entries.items():
            if not entry.verify():
                yield filename

if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument('command')
    parser.add_argument('directory', default='.')
    args = parser.parse_args()
    db = HashDatabase(args.directory)
    if args.command == 'init':
        db.update()
        db.save()
    if args.command == 'update':
        db.load()
        db.update()
        db.save()
    if args.command == 'verify':
        db.load()
        for failed_filename in db.verify():
            print('failed: {}'.format(failed_filename))
