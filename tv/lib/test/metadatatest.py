import collections
import itertools
import os
import urllib
import random
import string
import json

from miro.test import mock
from miro.test.framework import MiroTestCase, EventLoopTest
from miro import app
from miro import databaseupgrade
from miro import echonest
from miro import prefs
from miro import schema
from miro import filetypes
from miro import metadata
from miro import workerprocess
from miro.plat import resources
from miro.plat.utils import (PlatformFilenameType,
                             get_enmfp_executable_path)

class MockMetadataProcessor(object):
    """Replaces the mutagen and movie data code with test values."""
    def __init__(self, cover_art_dir):
        self.reset()
        self.cover_art_dir = cover_art_dir

    def reset(self):
        self.mutagen_calls = {}
        self.movie_data_calls = {}
        self.echonest_codegen_calls = {}
        self.canceled_files = set()

    def send(self, task, callback, errback):
        task_data = (task, callback, errback)

        if isinstance(task, workerprocess.MutagenTask):
            if task.source_path in self.mutagen_calls:
                raise ValueError("Already processing %s" % task.source_path)
            self.mutagen_calls[task.source_path] = task_data
        elif isinstance(task, workerprocess.MovieDataProgramTask):
            if task.source_path in self.movie_data_calls:
                raise ValueError("Already processing %s" % task.source_path)
            self.movie_data_calls[task.source_path] = task_data
        elif isinstance(task, workerprocess.CancelFileOperations):
            self.canceled_files.update(task.paths)
        else:
            raise TypeError(task)

    def exec_codegen(self, codegen_path, path, callback, errback):
        self.echonest_codegen_calls[path] = (callback, errback)

    def run_mutagen_callback(self, source_path, file_type, duration, title,
                             album, drm, cover_art):
        task, callback, errback = self.mutagen_calls.pop(source_path)
        data = {
            'source_path': task.source_path,
            'file_type': unicode(file_type),
            'duration': duration,
            'title': unicode(title) if title is not None else None,
            'album': unicode(album) if album is not None else None,
            'drm': drm,
        }
        # Real mutagen calls send much more than the title for metadata, but
        # this is enough to test
        if cover_art and album is not None:
            # make sure there's a file where MetadataManager expects mutagen
            # to put the cover art
            cover_art_path = self._cover_art_path(album)
            open(cover_art_path, 'wb').write("FAKE FILE")

        # remove None values before sending the data
        for key, value in data.items():
            if value is None:
                del data[key]
        callback(task, data)

    def _cover_art_path(self, album_name):
        return os.path.join(self.cover_art_dir,
                            urllib.quote(album_name, safe=" ,"))

    def run_mutagen_errback(self, source_path, error):
        task, callback, errback = self.mutagen_calls.pop(source_path)
        errback(task, error)

    def run_movie_data_callback(self, source_path, file_type, duration,
                                screenshot_worked):
        try:
            task, callback, errback = self.movie_data_calls.pop(source_path)
        except KeyError:
            raise ValueError("No movie data run scheduled for %s" %
                             source_path)
        data = {
            'source_path': task.source_path,
            'file_type': unicode(file_type),
            'duration': duration,
        }
        if screenshot_worked:
            screenshot_path = self.get_screenshot_path(task.source_path)
            data['screenshot_path'] = screenshot_path
        # remove None values before sending the data
        for key, value in data.items():
            if value is None:
                del data[key]
        callback(task, data)

    def run_movie_data_errback(self, source_path, error):
        task, callback, errback = self.movie_data_calls.pop(source_path)
        errback(task, error)

    def run_echonest_codegen_callback(self, source_path, code):
        callback, errback = self.echonest_codegen_calls.pop(source_path)
        callback(source_path, code)

    def run_echonest_codegen_errback(self, source_path, error):
        callback, errback = self.echonest_codegen_calls.pop(source_path)
        errback(source_path, error)

    def get_screenshot_path(self, source_path):
        filename = os.path.basename(source_path) + ".png"
        return '/tmp/' + filename

class MetadataManagerTest(MiroTestCase):
    # Test the MetadataManager class
    def setUp(self):
        MiroTestCase.setUp(self)
        self.mutagen_data = collections.defaultdict(dict)
        self.movieprogram_data = collections.defaultdict(dict)
        self.user_info_data = collections.defaultdict(dict)
        self.echonest_codes = {}
        self.processor = MockMetadataProcessor(self.tempdir)
        self.patch_function('miro.workerprocess.send', self.processor.send)
        self.patch_function('miro.echonest.exec_codegen',
                            self.processor.exec_codegen)
        self.metadata_manager = metadata.MetadataManager(self.tempdir)

    def _calc_correct_metadata(self, path):
        """Calculate what the metadata should be for a path."""
        metadata = {
            'file_type': filetypes.item_file_type_for_filename(path),
        }
        metadata.update(self.mutagen_data[path])
        metadata.update(self.movieprogram_data[path])
        metadata.update(self.user_info_data[path])
        if 'album' in metadata:
            cover_art_path = self.cover_art_for_album(metadata['album'])
            if cover_art_path:
                metadata['cover_art_path'] = cover_art_path
        return metadata

    def cover_art_for_album(self, album_name):
        cover_art_path = None
        for metadata in self.mutagen_data.values():
            if ('album' in metadata and 'cover_art_path' in metadata and
                metadata['album'] == album_name):
                if (cover_art_path is not None and
                    metadata['cover_art_path'] != cover_art_path):
                    raise AssertionError("Different cover_part_paths for " +
                                         album_name)
                cover_art_path = metadata['cover_art_path']
        return cover_art_path

    def check_metadata(self, path):
        correct_metadata = self._calc_correct_metadata(path)
        self.metadata_manager._process_metadata_finished()
        self.metadata_manager._process_metadata_errors()
        metadata = self.metadata_manager.get_metadata(path)
        # don't check has_drm, we have a special test for that
        for dct in (metadata, correct_metadata):
            for key in ('has_drm', 'drm'):
                if key in dct:
                    del dct[key]
        self.assertDictEquals(metadata, correct_metadata)
        self.check_echonest_code(path)

    def check_echonest_code(self, path):
        status = metadata.MetadataStatus.get_by_path(path)
        if path not in self.echonest_codes:
            if path in self.metadata_manager.echonest_codes:
                raise AsssertionError("MetadataManager has code for %s" %
                                      path)
        elif isinstance(self.echonest_codes[path], Exception):
            self.assertEquals(status.echonest_status,
                              metadata.MetadataStatus.STATUS_FAILURE)
            if path in self.metadata_manager.echonest_codes:
                raise AsssertionError("MetadataManager has code for %s" %
                                      path)
        else:
            self.assertEquals(self.metadata_manager.echonest_codes[path],
                              self.echonest_codes[path])

    def make_path(self, filename):
        """Create a pathname for that file in the "/videos" directory
        """
        if not filename.startswith('/'):
            return '/videos/' + filename
        else:
            # filename is already absolute
            return filename

    def check_add_file(self, filename):
        path = self.make_path(filename)
        # before we add the path, get_metadata() should raise a KeyError
        self.assertRaises(KeyError, self.metadata_manager.get_metadata, path)
        # after we add the path, we should have only have metadata that we can
        # guess from the file
        self.metadata_manager.add_file(path)
        self.check_metadata(path)
        # after we add the path, calling add file again should raise a
        # ValueError
        self.assertRaises(ValueError, self.metadata_manager.add_file, path)

    def check_run_mutagen(self, filename, file_type, duration, title,
                          album=None, drm=False, cover_art=True):
        path = self.make_path(filename)
        mutagen_data = {
            'file_type': file_type,
            'duration': duration,
            'title': title,
            'album': album,
            'drm': drm,
        }
        # Remove None keys
        for k in mutagen_data.keys():
            if mutagen_data[k] is None:
                del mutagen_data[k]
        if cover_art and album is not None:
            mutagen_data['cover_art_path'] = \
                    self.processor._cover_art_path(album)
        self.mutagen_data[path] = mutagen_data
        self.processor.run_mutagen_callback(path, file_type, duration, title,
                                            album, drm, cover_art)
        self.check_metadata(path)

    def check_queued_mutagen_calls(self, filenames):
        correct_keys = ['/videos/' + f for f in filenames]
        self.assertSameSet(correct_keys, self.processor.mutagen_calls.keys())

    def check_queued_moviedata_calls(self, filenames):
        correct_keys = ['/videos/' + f for f in filenames]
        self.assertSameSet(correct_keys,
                           self.processor.movie_data_calls.keys())

    def check_queued_echonest_codegen_calls(self, filenames):
        correct_keys = ['/videos/' + f for f in filenames]
        self.assertSameSet(correct_keys,
                           self.processor.echonest_codegen_calls.keys())

    def get_metadata(self, filename):
        path = self.make_path(filename)
        return self.metadata_manager.get_metadata(path)

    def check_mutagen_error(self, filename):
        path = self.make_path(filename)
        self.processor.run_mutagen_errback(path, ValueError())
        # mutagen failing shouldn't change the metadata
        self.check_metadata(path)

    def check_movie_data_not_scheduled(self, filename):
        if self.make_path(filename) in self.processor.movie_data_calls:
            raise AssertionError("movie data scheduled for %s" % filename)

    def check_run_movie_data(self, filename, file_type, duration,
                             screenshot_worked):
        path = self.make_path(filename)
        self.processor.run_movie_data_callback(path, file_type, duration,
                                               screenshot_worked)
        # check that the metadata is updated based on the values from mutagen
        movieprogram_data = {
            'file_type': file_type,
            'duration': duration,
        }
        # remove keys with null values
        for key in movieprogram_data.keys():
            if movieprogram_data[key] is None:
                del movieprogram_data[key]
        if screenshot_worked:
            movieprogram_data['screenshot_path'] = \
                    self.processor.get_screenshot_path(path)
        self.movieprogram_data[path] = movieprogram_data
        self.check_metadata(path)

    def check_movie_data_error(self, filename):
        path = self.make_path(filename)
        self.processor.run_movie_data_errback(path, ValueError())
        # movie data failing shouldn't change the metadata
        self.check_metadata(path)

    def check_echonest_not_scheduled(self, filename):
        self.check_echonest_not_running(filename)
        path = self.make_path(filename)
        status = metadata.MetadataStatus.get_by_path(path)
        self.assertEquals(status.echonest_status, status.STATUS_SKIP)

    def check_echonest_not_running(self, filename):
        path = self.make_path(filename)
        if path in self.processor.echonest_codegen_calls:
            raise AssertionError("echonest_codegen scheduled for %s" %
                                 filename)

    def check_both_failure(self, filename):
        self.check_add_file('foo.avi')
        self.check_mutagen_error('foo.avi')
        self.check_movie_data_error('foo.avi')

    def calc_fake_echonest_code(self, path):
        """Echoprint codes are huge strings of ascii data.  Generate a unique
        one for a path.
        """
        random.seed(path)
        length = random.randint(3000, 4000)
        return ''.join(random.choice(string.ascii_letters)
                       for i in xrange(length))

    def check_run_echonest_codegen(self, filename):
        path = self.make_path(filename)
        code = self.calc_fake_echonest_code(path)
        self.echonest_codes[path] = code
        self.processor.run_echonest_codegen_callback(path, code)
        self.check_metadata(path)

    def check_echonest_codegen_error(self, filename):
        path = self.make_path(filename)
        error = IOError()
        self.echonest_codes[path] = error
        self.processor.run_echonest_codegen_errback(path, error)
        self.check_metadata(path)

    def check_set_user_info(self, filename, **info):
        path = self.make_path(filename)
        self.user_info_data[path].update(info)
        self.metadata_manager.set_user_data(path, info)
        self.check_metadata(path)

    def test_video(self):
        # Test video files with no issuse
        self.check_add_file('foo.avi')
        self.check_run_mutagen('foo.avi', 'video', 101, 'Foo', 'Fight Vids')
        self.check_run_movie_data('foo.avi', 'video', 100, True)
        self.check_echonest_not_scheduled('foo.avi')

    def test_video_no_screenshot(self):
        # Test video files where the movie data program fails to take a
        # screenshot
        self.check_add_file('foo.avi')
        self.check_run_mutagen('foo.avi', 'video', 100, 'Foo')
        self.check_run_movie_data('foo.avi', 'video', 100, False)
        self.check_echonest_not_scheduled('foo.avi')

    def test_audio(self):
        # Test audio files with no issuse
        self.check_add_file('foo.mp3')
        self.check_run_mutagen('foo.mp3', 'audio', 200, 'Bar', 'Fights')
        self.check_movie_data_not_scheduled('foo.mp3')
        self.check_run_echonest_codegen('foo.mp3')

    def test_echonest_codegen_error(self):
        # Test audio files that echonest_codegen bails on
        self.check_add_file('foo.mp3')
        self.check_run_mutagen('foo.mp3', 'audio', 200, 'Bar', 'Fights')
        self.check_movie_data_not_scheduled('foo.mp3')
        self.check_echonest_codegen_error('foo.mp3')

    def test_echonest_codegen_config(self):
        # test echonest preference stops echonest_codegen from running
        app.config.set(prefs.ECHONEST_ENABLED, False)
        self.check_add_file('foo.mp3')
        self.check_run_mutagen('foo.mp3', 'audio', 200, 'Bar', 'Fights')
        self.check_movie_data_not_scheduled('foo.mp3')
        self.check_echonest_not_running('foo.mp3')
        app.config.set(prefs.ECHONEST_ENABLED, True)
        self.check_run_echonest_codegen('foo.mp3')

    def test_audio_shares_cover_art(self):
        # Test that if one audio file in an album has cover art, they all will
        self.check_add_file('foo.mp3')
        self.check_run_mutagen('foo.mp3', 'audio', 200, 'Bar', 'Fights')
        self.check_add_file('foo2.mp3')
        self.check_run_mutagen('foo2.mp3', 'audio', 300, 'Foo', 'Fights',
                               cover_art=False)
        self.check_add_file('foo3.mp3')
        self.check_run_mutagen('foo3.mp3', 'audio', 400, 'Baz', 'Fights',
                               cover_art=False)

    def test_audio_no_duration(self):
        # Test audio files where mutagen can't get the duration
        self.check_add_file('foo.mp3')
        self.check_run_mutagen('foo.mp3', 'audio', None, 'Bar', 'Fights')
        # Because mutagen failed to get the duration, we should have a movie
        # data call scheduled
        self.check_run_movie_data('foo.mp3', 'audio', 100, False)
        self.check_run_echonest_codegen('foo.mp3')

    def test_audio_no_duration2(self):
        # same as test_audio_no_duration, but have movie data return that the
        # file is actually a video file.  In this case, we shouldn't run
        # echonest_codegen
        self.check_add_file('foo.mp3')
        self.check_run_mutagen('foo.mp3', 'audio', None, 'Bar', 'Fights')
        # Because mutagen failed to get the duration, we should have a movie
        # data call scheduled
        self.check_run_movie_data('foo.mp3', 'video', 100, False)
        # since movie data returned video, we shouldn't run echonest_codegen
        self.check_echonest_not_scheduled('foo.mp3')

    def test_ogg(self):
        # Test ogg files
        self.check_add_file('foo.ogg')
        self.check_run_mutagen('foo.ogg', 'audio', 100, 'Bar', 'Fights')
        # Even though mutagen thinks this file is audio, we should still run
        # mutagen because it might by a mis-identified ogv file
        self.check_run_movie_data('foo.ogg', 'video', 100, True)
        self.check_echonest_not_scheduled('foo.ogg')

    def test_other(self):
        # Test non media files
        self.check_add_file('foo.pdf')
        self.check_run_mutagen('foo.pdf', 'other', None, None, None)
        # Since mutagen couldn't determine the file type, we should run movie
        # data
        self.check_run_movie_data('foo.pdf', 'other', None, False)
        # since neither could determine the filename, we shouldn't run
        # echonest_codegen
        self.check_echonest_not_scheduled('foo.pdf')

    def test_mutagen_failure(self):
        # Test mutagen failing
        self.check_add_file('foo.avi')
        self.check_mutagen_error('foo.avi')
        # We should run movie data since mutagen failed
        self.check_run_movie_data('foo.avi', 'other', 100, True)
        self.check_echonest_not_scheduled('foo.avi')

    def test_movie_data_failure(self):
        # Test video files where movie data fails
        self.check_add_file('foo.avi')
        self.check_run_mutagen('foo.avi', 'video', 100, 'Foo')
        self.check_movie_data_error('foo.avi')

    def test_has_drm(self):
        # check the has_drm flag
        self.check_add_file('foo.avi')
        self.check_run_mutagen('foo.avi', 'audio', 100, 'Foo', 'Fighters',
                               drm=True)
        # if mutagen thinks a file has drm, we still need to check with movie
        # data to make sure
        self.assertEquals(self.get_metadata('foo.avi')['has_drm'], False)
        # if we get a movie data error, than we know there's DRM
        self.check_movie_data_error('foo.avi')
        self.assertEquals(self.get_metadata('foo.avi')['has_drm'], True)

        # let's try that whole process again, but make movie data succeed.  In
        # that case has_drm should be false
        self.check_add_file('foo2.avi')
        self.check_run_mutagen('foo2.avi', 'audio', 100, 'Foo', 'Fighters',
                               drm=True)
        self.assertEquals(self.get_metadata('foo2.avi')['has_drm'], False)
        self.check_run_movie_data('foo2.avi', 'audio', 100, True)
        self.assertEquals(self.get_metadata('foo2.avi')['has_drm'], False)

    def test_restart_incomplete(self):
        # Test restarting incomplete 
        self.check_add_file('foo.avi')
        self.check_run_mutagen('foo.avi', 'video', 100, 'Foo')
        self.check_add_file('bar.avi')
        self.check_add_file('baz.mp3')
        self.check_run_mutagen('baz.mp3', 'audio', 100, 'Foo', 'Fighters')
        self.check_add_file('qux.avi')
        self.check_run_mutagen('qux.avi', 'video', 100, 'Foo')
        self.check_run_movie_data('qux.avi', 'video', 100, True)
        # At this point, foo is waiting for moviedata, bar is waiting for
        # mutagen and baz is waiting for echonest_codegen.
        self.check_queued_moviedata_calls(['foo.avi'])
        self.check_queued_mutagen_calls(['bar.avi'])
        self.check_queued_echonest_codegen_calls(['baz.mp3'])
        # Check that if we call restart_incomplete now, we don't get queue
        # mutagen or movie data twice.
        self.processor.reset()
        self.metadata_manager.restart_incomplete()
        self.check_queued_moviedata_calls([])
        self.check_queued_mutagen_calls([])
        self.check_queued_echonest_codegen_calls([])
        # Create a new MetadataManager and call restart_incomplete on that.
        # That should invoke mutagen and movie data
        self.metadata_manager = metadata.MetadataManager(self.tempdir)
        self.metadata_manager.restart_incomplete()
        self.check_queued_moviedata_calls(['foo.avi'])
        self.check_queued_mutagen_calls(['bar.avi'])
        self.check_queued_echonest_codegen_calls(['baz.mp3'])
        # Theck that when things finish, we get other incomplete metadata
        self.check_run_mutagen('bar.avi', 'audio', None, 'Foo')
        self.check_queued_moviedata_calls(['foo.avi', 'bar.avi'])
        self.check_run_movie_data('bar.avi', 'audio', 100, 'Foo')
        self.check_run_echonest_codegen('baz.mp3')
        self.check_queued_echonest_codegen_calls(['bar.avi'])

    def check_path_in_system(self, filename, correct_value):
        path = self.make_path(filename)
        self.assertEquals(self.metadata_manager.path_in_system(path),
                          correct_value)

    def test_path_in_system(self):
        # Test the path_in_system() call
        self.check_add_file('foo.avi')
        self.check_run_mutagen('foo.avi', 'video', 100, 'Foo')
        self.check_add_file('bar.avi')
        self.check_add_file('baz.mp3')
        self.check_run_mutagen('baz.mp3', 'audio', 100, 'Foo', 'Fighters')
        self.check_add_file('qux.avi')
        self.check_run_mutagen('qux.avi', 'video', 100, 'Foo')
        self.check_run_movie_data('qux.avi', 'video', 100, True)
        self.check_path_in_system('foo.avi', True)
        self.check_path_in_system('bar.avi', True)
        self.check_path_in_system('baz.mp3', True)
        self.check_path_in_system('qux.avi', True)
        self.check_path_in_system('other-file.avi', False)
        # Test path_in_system() for objects in the DB, but not in cache
        self.clear_ddb_object_cache()
        self.metadata_manager = metadata.MetadataManager(self.tempdir)
        self.check_path_in_system('foo.avi', True)
        self.check_path_in_system('bar.avi', True)
        self.check_path_in_system('baz.mp3', True)
        self.check_path_in_system('qux.avi', True)
        self.check_path_in_system('other-file.avi', False)

    def test_delete(self):
        # add a couple files at different points in the metadata process
        self.check_add_file('foo.avi')
        self.check_run_mutagen('foo.avi', 'video', 100, 'Foo')
        self.check_add_file('bar.mp3')
        self.check_add_file('baz.avi')
        self.check_run_mutagen('baz.avi', 'video', 100, 'Foo')
        self.check_run_movie_data('baz.avi', 'video', 100, True)
        self.check_queued_moviedata_calls(['foo.avi'])
        self.check_queued_mutagen_calls(['bar.mp3'])
        # remove the files
        to_remove = ['/videos/foo.avi', '/videos/bar.mp3', '/videos/baz.avi' ]
        self.metadata_manager.remove_file(to_remove[0])
        self.metadata_manager.remove_files(to_remove[1:])
        # check that the metadata manager sent a CancelFileOperations message
        self.assertEquals(self.processor.canceled_files, set(to_remove))
        # check that none of the videos are in the metadata manager
        for path in to_remove:
            self.assertRaises(KeyError, self.metadata_manager.get_metadata,
                              path)
        # check that callbacks/errbacks for those files don't result in
        # errors.  The metadata system may have already been processing the
        # file when it got the CancelFileOperations message.
        self.processor.run_movie_data_callback('/videos/foo.avi', 'video',
                                               100, True)
        self.processor.run_mutagen_errback('/videos/bar.mp3', ValueError())

    def test_user_and_torrent_data(self):
        self.check_add_file('foo.avi')
        self.check_set_user_info('foo.avi', title=u'New Foo',
                                 album=u'The best')
        self.check_run_mutagen('foo.avi', 'video', 100, 'Foo')
        self.check_set_user_info('foo.avi', title=u'Newer Foo')
        self.check_run_movie_data('foo.avi', 'video', 100, True)
        self.check_set_user_info('foo.avi', album=u'The bestest')
        # check the final metadata one last time
        metadata = self.get_metadata('foo.avi')
        self.assertEquals(metadata['title'], 'Newer Foo')
        self.assertEquals(metadata['album'], 'The bestest')

    def test_queueing(self):
        # test that if we don't send too many requests to the worker process
        paths = ['/videos/video-%d.avi' % i for i in xrange(200)]

        def run_mutagen(start, stop):
            for p in paths[start:stop]:
                # this ensures that both moviedata and echonest will be run
                # for this file
                self.processor.run_mutagen_callback(p, 'audio', None,
                                                    u'Title', u'Album', False,
                                                    False)
        def run_movie_data(start, stop):
            for p in paths[start:stop]:
                self.processor.run_movie_data_callback(p, 'audio', 100, True)

        def run_echonest_codegen(start, stop):
            for p in paths[start:stop]:
                code = self.calc_fake_echonest_code(p)
                self.processor.run_echonest_codegen_callback(p, code)

        def check_counts(mutagen_calls, movie_data_calls,
                         echonest_codegen_calls):
            self.metadata_manager._process_metadata_finished()
            self.metadata_manager._process_metadata_errors()
            self.assertEquals(len(self.processor.mutagen_calls),
                              mutagen_calls)
            self.assertEquals(len(self.processor.movie_data_calls),
                              movie_data_calls)
            self.assertEquals(len(self.processor.echonest_codegen_calls),
                              echonest_codegen_calls)

        # Add all 200 paths to the metadata manager.  Only 100 should be
        # queued up to mutagen
        for p in paths:
            self.metadata_manager.add_file(p)
        check_counts(100, 0, 0)

        # let 50 mutagen tasks complete, we should queue up 50 more
        run_mutagen(0, 50)
        check_counts(100, 50, 0)
        # let 75 more complete, we should be hitting our movie data max now
        run_mutagen(50, 125)
        check_counts(75, 100, 0)
        # run a bunch of movie data calls.  This will let us test the echonest
        # queueing
        run_movie_data(0, 100)
        # we should only have 1 echonest codegen program running at once
        check_counts(75, 25, 1)
        # we should stop running echonest codegen once we have 5 codes queued
        # up
        run_echonest_codegen(0, 5)
        check_counts(75, 25, 0)
        # looks good, just double check that we finish our queues okay
        run_mutagen(125, 200)
        check_counts(0, 100, 0)
        run_movie_data(100, 200)
        check_counts(0, 0, 0)

    def test_move(self):
        # add a couple files at different points in the metadata process
        self.check_add_file('foo.avi')
        self.check_run_mutagen('foo.avi', 'video', 100, 'Foo')
        self.check_add_file('bar.mp3')
        self.check_add_file('baz.avi')
        self.check_run_mutagen('baz.avi', 'video', 100, 'Foo')
        self.check_run_movie_data('baz.avi', 'video', 100, True)
        self.check_queued_moviedata_calls(['foo.avi'])
        self.check_queued_mutagen_calls(['bar.mp3'])
        # Move some of the files to new names
        def new_path_name(old_path):
            return '/videos2/' + os.path.basename(old_path)
        to_move = ['/videos/foo.avi', '/videos/bar.mp3', '/videos/baz.avi' ]
        old_metadata = dict((p, self.metadata_manager.get_metadata(p))
                             for p in to_move)
        self.metadata_manager.will_move_files(to_move)
        # check that the metadata manager sent a CancelFileOperations message
        self.assertEquals(self.processor.canceled_files, set(to_move))
        # tell metadata manager that the move is done
        for path in to_move:
            self.metadata_manager.file_moved(path, new_path_name(path))
        # check that the metadata stored with the new path and not the old one
        for path in to_move:
            new_path = new_path_name(path)
            for dct in (self.mutagen_data, self.movieprogram_data,
                        self.user_info_data):
                dct[new_path] = dct.pop(path)
            self.assertEquals(old_metadata[path],
                              self.metadata_manager.get_metadata(new_path))
            self.assertRaises(KeyError, self.metadata_manager.get_metadata,
                              path)
        # check that callbacks/errbacks for the old paths don't result in
        # errors.  The metadata system may have already been processing the
        # file when it got the CancelFileOperations message.
        self.processor.run_movie_data_callback('/videos/foo.avi', 'video',
                                               100, True)
        self.processor.run_mutagen_errback('/videos/bar.mp3', ValueError())
        # check that callbacks work for new paths
        self.check_run_movie_data('/videos2/foo.avi', 'video', 100, True)
        self.check_run_mutagen('/videos2/bar.mp3', 'audio', 120, 'Bar',
                               'Fights')

    def test_queueing_with_delete(self):
        # test that we remove files that are queued as well
        paths = ['/videos/video-%d.avi' % i for i in xrange(200)]
        for p in paths:
            self.metadata_manager.add_file(p)
        # we now have 200 mutagen calls so 100 of them should be pending

        # if some files get removed, then we should start new ones
        self.metadata_manager.remove_files(paths[:25])
        self.assertEquals(len(self.processor.mutagen_calls), 125)

        # If pending files get removed, we should remove them from the pending
        # queues
        self.metadata_manager.remove_files(paths[25:])
        mm = self.metadata_manager
        self.assertEquals(len(mm.mutagen_processor._pending_tasks), 0)
        self.assertEquals(len(mm.moviedata_processor._pending_tasks), 0)

    def test_queueing_with_move(self):
        # test moving queued files
        paths = ['/videos/video-%d.avi' % i for i in xrange(200)]
        for p in paths:
            self.metadata_manager.add_file(p)
        # we now have 200 mutagen calls so 100 of them should be pending

        # if pending files get moved, the paths should be updated
        moved = paths[150:]
        new_paths = ['/new' + p for p in moved]
        self.metadata_manager.will_move_files(moved)
        for old_path, new_path in zip(moved, new_paths):
            self.metadata_manager.file_moved(old_path, new_path)
        # send mutagen call backs so the pending calls start
        for p in paths[:100]:
            self.processor.run_mutagen_callback(p, 'video', 100, u'Title',
                                                u'Album', False, False)
        correct_paths = paths[100:150] + new_paths
        self.assertSameSet(self.processor.mutagen_calls.keys(), correct_paths)

class TestCodegen(EventLoopTest):
    def setUp(self):
        EventLoopTest.setUp(self)
        self.callback_data = self.errback_data = None
        self.codegen_path = get_enmfp_executable_path()

    def callback(self, *args):
        self.callback_data = args
        self.stopEventLoop(abnormal=False)

    def errback(self, *args):
        self.errback_data = args
        self.stopEventLoop(abnormal=False)

    def run_codegen(self, song_path):
        echonest.exec_codegen(self.codegen_path, song_path,
                              self.callback, self.errback)
        self.processThreads()
        self.runEventLoop()

    def test_codegen(self):
        song_path = resources.path('testdata/Wikipedia_Song_by_teddy.ogg')
        self.run_codegen(song_path)

        self.assertEquals(self.errback_data, None)
        correct_code = ('eJwdkIkRBCEIBFPiEzEcRDb_EG64LavV5pFaov8nAejA5moFrD'
                        'n6YE8gBkeAnFM58Cb5JdBwLHCsg6liH7cbOOjHiTyexlwI84eA'
                        'TDuZ18R9phicJn7r1afGwXvtrfSZ03qLUvVB0mWJ-gwjS1mqyK'
                        'KGVDlxTAOVlS4LXR9tOdT3nGvMzprtrl4rrC_nfReS8nOs0q1y'
                        'X17Z8aryw34aEnmnceG3PXuHRuyFPIRaIEkF8-IPmVFd5Mdhhi'
                        'S9LmYmndQvMEfdDL3aiECqoAryB-OLX8E=')
        self.assertEquals(self.callback_data, (song_path, correct_code))

    def test_codegen_error(self):
        song_path =resources.path('/file/not/found')
        self.run_codegen(song_path)
        self.assertEquals(self.callback_data, None)
        self.assertEquals(self.errback_data[0], song_path)
        self.assert_(isinstance(self.errback_data[1], Exception))
