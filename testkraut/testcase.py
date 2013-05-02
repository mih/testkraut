# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the testkraut package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Unittest compliant API for testkraut test cases"""

__docformat__ = 'restructuredtext'

import os
from os.path import join as opj
from json import dumps as jds
from functools import wraps

import logging
lgr = logging.getLogger(__name__)

from testtools import TestCase, RunTest
from testtools.content import Content, text_content
from testtools.content_type import ContentType, UTF8_TEXT
from testtools import matchers as tm
from testtools.matchers import Equals, Annotate, FileExists

from .utils import get_test_library_paths, describe_system
#from .utils import run_command, get_shlibdeps, which, sha1sum, \
#        get_script_interpreter, describe_system, get_test_library_paths
from .spec import SPEC, SPECJSONEncoder

#
# Utility code for template-based test cases
#
def TestArgs(*args, **kwargs):
    """Little helper to specify test arguments"""
    return (args, kwargs)

def template_case(args):
    def wrapper(func):
        func.template = args
        return func
    return wrapper

def _method_partial(func, *parameters, **kparms):
    @wraps(func)
    def wrapped(self, *args, **kw):
        kw.update(kparms)
        return func(self, *(args + parameters), **kw)
    return wrapped

class TemplateTestCase(type):
    """
    Originally based on code from
    https://bitbucket.org/lothiraldan/unittest-templates

    Copyright 2011, Boris Feld <http://boris.feld.crealio.fr>
    License: DTFYWTPL
    """
    #            DO WHAT THE FUCK YOU WANT TO PUBLIC LICENSE
    #                Version 2, December 2004
    #
    # Copyright (C) 2004 Sam Hocevar <sam@hocevar.net>
    #
    # Everyone is permitted to copy and distribute verbatim or modified
    # copies of this license document, and changing it is allowed as long
    # as the name is changed.
    #
    #            DO WHAT THE FUCK YOU WANT TO PUBLIC LICENSE
    #   TERMS AND CONDITIONS FOR COPYING, DISTRIBUTION AND MODIFICATION
    #
    #  0. You just DO WHAT THE FUCK YOU WANT TO.
    def __new__(cls, name, bases, attr):
        new_methods = {}
        for method_name in attr:
            if hasattr(attr[method_name], "template"):
                source = attr[method_name]
                source_name = method_name.lstrip("_")
                for test_name, args in source.template.items():
                    parg, kwargs = args
                    new_name = "test_%s" % test_name
                    new_methods[new_name] = _method_partial(source, *parg, **kwargs)
                    new_methods[new_name].__name__ = str(new_name)
        attr.update(new_methods)
        return type(name, bases, attr)

def discover_specs(paths=None):
    """Helper function to discover test SPECs in configured library locations
    """
    discovered = {}
    # for all configured test library locations
    if paths is None:
        paths = get_test_library_paths()
    for tld in paths:
        # for all subdirs
        for subdir in [d for d in os.listdir(tld)
                            if os.path.isdir(opj(tld, d))]:
            spec_fname = opj(tld, subdir, 'spec.json')
            if not os.path.isfile(spec_fname):
                # not a test spec
                lgr.debug("ignoring '%s' directory in library path '%s': contains no SPEC file"
                          % (subdir, tld))
                continue
            try:
                spec = SPEC(open(spec_fname))
                spec_id = spec['id'].replace('-', '_')
            except:
                # not a valid SPEC
                lgr.warning("ignoring '%s': no a valid SPEC file"
                          % spec_fname)
            if spec_id in discovered:
                lgr.warning("found duplicate test ID '%s' in %s: ignoring the latter test"
                            % (spec_id, (discovered[spec_id], spec_fname)))
                continue
            # we actually found a new one
            lgr.debug("discovered test SPEC '%s'" % spec_id)
            discovered[spec_id] = spec_fname
    # wrap spec file locations in TestArgs
    return dict([(k, TestArgs(v)) for k, v in discovered.iteritems()])


class TestFromSPEC(TestCase):
    __metaclass__ = TemplateTestCase

    _system_info = None

    def __init__(self, *args, **kwargs):
        TestCase.__init__(self, *args, **kwargs)
        self._workdir = None

# derived classes should have this
#    @template_case(discover_specs())
    def _run_spec_test(self, spec_filename):
        wdir = self._workdir
        # get the SPEC
        spec = SPEC(open(spec_filename))
        spec_id = spec['id']
        details = self.getDetails()
        ct = ContentType('application', 'json')
        # for some strange reason doing the following in a loop doesn't work at
        # all, all details become identical
        details['sys'] = Content(ct, lambda: [jds(self._get_system_info())])
        details['env'] = Content(ct, lambda: [jds(self._get_environment(spec))])
        #os.environ['TESTKRAUT_TESTBED_PATH'] = testbed_path

        # prepare the testbed, place test input into testbed
        from .runner import prepare_local_testbed
        prepare_local_testbed(spec, wdir, get_test_library_paths(),
                              cachedir=None, lazy=False)
        # execute the actual test implementation
        self._execute_any_test_implementation(spec)
        # check for expected output
        self._check_output_presence(spec)

    def setUp(self):
        """Runs prior each test run"""
        super(TestFromSPEC, self).setUp()
        import tempfile
        # check if we have a concurent test run
        assert(self._workdir is None)
        self._workdir = tempfile.mkdtemp(prefix='testkraut')
        lgr.debug("created work dir at '%s'" % self._workdir)

    def tearDown(self):
        """Runs after each test run"""
        super(TestFromSPEC, self).tearDown()
        if not self._workdir is None:
            lgr.debug("remove work dir at '%s'" % self._workdir)
            import shutil
            shutil.rmtree(self._workdir)
            self._workdir = None

    def _execute_any_test_implementation(self, spec):
        type_ = spec['test']['type']
        try:
            test_exec = getattr(self, '_execute_%s' % type_)
        except AttributeError:
            raise ValueError("unsupported test type '%s'" % type_)
        lgr.debug("run test via %s()" % test_exec.__name__)
        ret = test_exec(spec)
        return ret

    def _execute_shell_command(self, spec):
        import subprocess
        testspec = spec['test']
        cmd = testspec['command']
        if isinstance(cmd, list):
            # convert into a cmd string to execute via shell
            # to get all envvar expansion ...
            cmd = subprocess.list2cmdline(cmd)
        # for the rest we need to execute stuff in the root of the testbed
        initial_cwd = os.getcwdu()
        os.chdir(self._workdir)
        try:
            lgr.debug("attempting to execute command '%s'" % cmd)
            texec = subprocess.Popen(cmd,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    shell=True)
            texec.wait()
            # record the exit code
            testspec['exitcode'] = texec.returncode
            # store test output
            for chan in ('stderr', 'stdout'):
                testspec[chan] = getattr(texec, chan).read()
                lgr.debug('%s: %s' % (chan, testspec[chan]))
            self.assertThat(
                texec.returncode,
                Annotate("test shell command '%s' yielded non-zero exit code" % cmd,
                         Equals(0)))
        except OSError, e:
            lgr.error("%s: %s" % (e.__class__.__name__, str(e)))
            return False
        finally:
            os.chdir(initial_cwd)
        return False

    def _check_output_presence(self, spec):
        outspec = spec.get('outputs', {})
        unmatched_output = []
        for ospec_id in outspec:
            ospec = outspec[ospec_id]
            ospectype = ospec['type']
            if ospectype == 'file':
                self.assertThat(
                    ospec['value'],
                    Annotate('expected output file missing', FileExists()))
            elif ospectype == 'string':
                sec, field = ospec_id.split('::')
                self.assertThat(
                    spec[sec][field],
                    Annotate("unexpected output for '%s'" % ospec_id,
                             Equals(ospec['value'])))
            else:
                raise NotImplementedError(
                        "dunno how to handle output type '%s' yet"
                        % ospectype)
            # TODO check for file type

    def _get_system_info(self):
        if TestFromSPEC._system_info is None:
            TestFromSPEC._system_info = describe_system()
        return TestFromSPEC._system_info

    def _get_environment(self, spec):
        info = {}
        for env in spec.get('environment', {}):
            # grab envvar values
            info[env] = os.environ.get(env, 'UNDEFINED')
        return info

    def _gather_component_info(self, spec):
        info = {}
        entities = {}
        info['entities'] = entities
        for exec_path, espec in spec.get('executables', {}).iteritems():
            if not os.path.exists(os.path.expandvars(exec_path)):
                # no executable? is it optional?
                if not espec.get('optional', False):
                    lgr.warning("failed to find required executable '%s'"
                                % exec_path)
                continue
            # replace exectutable info with the full picture
            ehash = self._describe_binary(exec_path,
                                          entities,
                                          type_='binary')
            # link the old info with the new one
            espec['entity'] = ehash
            # check version information
            have_version = False
            if 'version_file' in espec:
                verfilename = espec['version_file']
                extract_regex = r'.*'
                if isinstance(verfilename, list):
                    verfilename, extract_regex = verfilename
                # expand the filename
                verfilename = os.path.realpath(os.path.expandvars(verfilename))
                try:
                    file_content = open(verfilename).read().strip()
                    version = re.findall(extract_regex, file_content)[0]
                    if len(version):
                        entities[ehash]['version'] = version
                        have_version = True
                except:
                    lgr.debug("failed to read version from '%s'"
                              % verfilename)
            if not have_version and 'version_cmd' in espec:
                vercmd = espec['version_cmd']
                extract_regex = r'.*'
                if isinstance(vercmd, list):
                    vercmd, extract_regex = vercmd
                ret = run_command(vercmd)
                try:
                    # this will throw an exception if nothing is found
                    version = re.findall(extract_regex, '\n'.join(ret['stderr']))[0]
                    if len(version):
                        entities[ehash]['version'] = version
                        have_version = True
                except:
                    try:
                        version = re.findall(extract_regex, '\n'.join(ret['stdout']))[0]
                        if len(version):
                            entities[ehash]['version'] = version
                            have_version = True
                    except:
                        lgr.debug("failed to read version from '%s'" % vercmd)

        if not len(entities):
            # remove unnecessary dict
            del info['entities']
        return info

