#!/usr/bin/env python

"""Communicating sequential processes, in Python.

When using CSP Python as a DSL, this module will normally be imported
via the statement 'from csp.csp import *' and should not be imported directly.

TODO: Write a Windows version of this file
TODO: http://sourceforge.net/projects/pywin32/
TODO: http://msdn.microsoft.com/en-us/library/ms810613.aspx
TODO: http://docs.python.org/library/mmap.html

Copyright (C) Sarah Mount, 2008-12.

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
02110-1301, USA
"""

__author__ = 'Sarah Mount <s.mount@wlv.ac.uk>'
__date__ = '2011-12-23'

# pylint: disable-msg=W0142

#DEBUG = True
DEBUG = False

from functools import wraps # Easy decorators

import copy
import gc
import inspect
import logging
import os
import random
import sys
import time
import uuid

from forking import Process
from synchronisation import Semaphore, SemNotAvailable, SharedMemory, Value, Lock


CSP_IMPLEMENTATION = 'os_posix'

print(CSP_IMPLEMENTATION)

### Names exported by this module
__all__ = ['set_debug', 'CSPProcess', 'CSPServer', 'Alt',
           'Par', 'Seq', 'Guard', 'Channel',
           'process', 'forever', 'Skip', '_CSPTYPES', 'CSP_IMPLEMENTATION']

### Seeded random number generator (16 bytes)

_RANGEN = random.Random(os.urandom(16))

### CONSTANTS

_debug = logging.debug


class CorruptedData(Exception):
    """Used to verify that data has come from an honest source.
    """

    def __str__(self):
        return 'Data sent with incorrect authentication key.'


class NoGuardInAlt(Exception):
    """Raised when an Alt has no guards to select.
    """

    def __str__(self):
        return 'Every Alt must have at least one guard.'


### Special constants / exceptions for termination and mobility
### Better not to use classes/objects here or pickle will get confused
### by the way that csp.__init__ manages the namespace.

_POISON = ';;;__POISON__;;;'
"""Used as special data sent down a channel to invoke termination."""


class ChannelPoison(Exception):
    """Used to poison a processes and propagate to all known channels.
    """

    def __str__(self):
        return 'Posioned channel exception.'


### DEBUGGING

def set_debug(status):
    global DEBUG
    DEBUG = status
    logging.basicConfig(level=logging.NOTSET,
                        stream=sys.stderr)
    logging.info("Using POSIX version of python-csp.")


### Fundamental CSP concepts -- Processes, Channels, Guards    
    
class _CSPOpMixin(object):
    """Mixin class used for operator overloading in CSP process types.
    """

    def __init__(self):
        return

    def referent_visitor(self, referents):
        for obj in referents:
            if obj is self or obj is None:
                continue
            if isinstance(obj, Channel):
                obj.poison()
            elif ((hasattr(obj, '__getitem__') or hasattr(obj, '__iter__')) and
                  not isinstance(obj, str)):
                self.referent_visitor(obj)
            elif isinstance(obj, CSPProcess):
                self.referent_visitor(obj.args + tuple(obj.kwargs.values()))
            elif hasattr(obj, '__dict__'):
                self.referent_visitor(list(obj.__dict__.values()))

    def __gt__(self, other):
        """Implementation of CSP Seq."""
        assert _is_csp_type(other)
        # FIXME: Only works for two processes.
        seq = Seq(self, Seq(other))
        seq.start()
        return seq

    def __mul__(self, n):
        assert n > 0
        procs = [self]
        for _ in range(n-1):
            procs.append(copy.copy(self))
        Seq(*procs).start()

    def __rmul__(self, n):
        assert n > 0
        procs = [self]
        for _ in range(n-1):
            procs.append(copy.copy(self))
        Seq(*procs).start()

    def __floordiv__(self, proclist):
        """
        Run this process in parallel with a list of others.
        """
        par = Par(self, *list(proclist))
        par.start()


class CSPProcess(Process, _CSPOpMixin):
    """Implementation of CSP processes.
    
    There are two ways to create a new CSP process. Firstly, you can
    use the @process decorator to convert a function definition into a
    CSP Process. Once the function has been defined, calling it will
    return a new CSPProcess object which can be started manually, or
    used in an expression:

>>> @process
... def foo(n):
...     print 'n:', n
... 
>>> foo(100).start()
>>> n: 100

>>> foo(10) // (foo(20),)
n: 10
n: 20
<Par(Par-5, initial)>
>>> 

    Alternatively, you can create a CSPProcess object directly and
    pass a function (and its arguments) to the CSPProcess constructor:

>>> def foo(n):
...     print 'n:', n
... 
>>> p = CSPProcess(foo, 100)
>>> p.start()
>>> n: 100

>>> 
    """

    def __init__(self, func, *args, **kwargs):
        Process.__init__(self,
                         target=func,
                         args=(args),
                         kwargs=kwargs)
        assert inspect.isfunction(func)   # Check we aren't using objects
        assert not inspect.ismethod(func) # Check we aren't using objects
        _CSPOpMixin.__init__(self)
        for arg in list(self._args) + list(self._kwargs.values()):
            if _is_csp_type(arg):
                arg.enclosing = self
        self.enclosing = None

    def getPid(self):
        return self._parent_pid

    def __str__(self):
        return 'CSPProcess running in PID {0}s'.format(self.getPid())

    def run(self): 
        """Called automatically when the L{start} methods is called.
        """
        try:
            self._target(*self._args, **self._kwargs)
        except ChannelPoison:
            _debug('{0}s got ChannelPoison exception in {1}'.format(str(self), self.getPid()))
            self.referent_visitor(self._args + tuple(self._kwargs.values()))
#            if self._started is not None: self.terminate()
        except KeyboardInterrupt:
            sys.exit()
        except Exception:
            typ, excn, tback = sys.exc_info()
            sys.excepthook(typ, excn, tback)

    def __del__(self):
        """Run the garbage collector automatically on deletion of this
        object.

        This prevents the "Winder Bug" found in tests/winder_bug of
        the distribution, where successive process graphs are created
        in memory and, when the "outer" CSPProcess object returns from
        its .start() method the process graph is not garbage
        collected. This accretion of garbage can cause degenerate
        behaviour which is difficult to debug, such as a program
        pausing indefinitely on Channel creation.
        """
        if gc is not None:
            gc.collect()


class CSPServer(CSPProcess):
    """Implementation of CSP server processes.
    Not intended to be used in client code. Use @forever instead.
    """

    def __init__(self, func, *args, **kwargs):
        CSPProcess.__init__(self, func, *args, **kwargs)

    def __str__(self):
        return 'CSPServer running in PID {0}s'.format(self.getPid())

    def run(self):
        """Called automatically when the L{start} methods is called.
        """
        try:
            generator = self._target(*self._args, **self._kwargs)
            while sys.gettrace() is None:
                next(generator)
            else:
                # If the tracer is running execute the target only once.
                next(generator)
                logging.info('Server process detected a tracer running.')
                # Be explicit.
                return None
        except ChannelPoison:
            _debug('{0}s in {1} got ChannelPoison exception'.format(str(self), self.getPid()))
            self.referent_visitor(self._args + tuple(self._kwargs.values()))
#            if self._started is not None: self.terminate()
        except KeyboardInterrupt:
            sys.exit()
        except Exception:
            typ, excn, tback = sys.exc_info()
            sys.excepthook(typ, excn, tback)


class Alt(_CSPOpMixin):
    """CSP select (OCCAM ALT) process.

    python-csp process will often have access to several different
    channels, or other guard types such as timer guards, and will have
    to choose one of them to read from. For example, in a
    producer/consumer or worker/farmer model, many producer or worker
    processes will be writing values to channels and one consumer or
    farmer process will be aggregating them in some way. It would be
    inefficient for the consumer or farmer to read from each channel
    in turn, as some channels might take longer than others. Instead,
    python-csp provides support for ALTing (or ALTernating), which
    enables a process to read from the first channel (or timer, or
    other guard) in a list to become ready.

    The simplest way to choose between channels (or other guards) is
    to use choice operator: "|", as in the example below:

>>> @process
... def send_msg(chan, msg):
...     chan.write(msg)
... 
>>> @process
... def choice(chan1, chan2):
...     # Choice chooses a channel on which to call read()
...     print chan1 | chan2
...     print chan1 | chan2
... 
>>> c1, c2 = Channel(), Channel()
>>> choice(c1, c2) // (send_msg(c1, 'yes'), send_msg(c2, 'no'))
yes
no
<Par(Par-8, initial)>
>>>

    Secondly, you can create an Alt object explicitly, and call its
    select() method to perform a channel read on the next available
    channel. If more than one channel is available to read from, then
    an available channel is chosen at random (for this reason, ALTing
    is sometimes called "non-deterministic choice":

>>> @process
... def send_msg(chan, msg):
...     chan.write(msg)
... 
>>> @process
... def alt_example(chan1, chan2):
...     alt = Alt(chan1, chan2)
...     print alt.select()
...     print alt.select()
... 
>>> c1, c2 = Channel(), Channel()
>>> Par(send_msg(c1, 'yes'), send_msg(c2, 'no'), alt_example(c1, c2)).start()
yes
no
>>>

    In addition to the select() method, which chooses an available
    guard at random, Alt provides two similar methods, fair_select()
    and pri_select(). fair_select() will choose not to select the
    previously selected guard, unless it is the only guard
    available. This ensures that no guard will be starved twice in a
    row. pri_select() will select available channels in the order in
    which they were passed to the Alt() constructor, giving a simple
    implementation of guard priority.

    Lastly, Alt() can be used with the repetition operator (*) to
    create a generator:

>>> @process
... def send_msg(chan, msg):
...     chan.write(msg)
... 
>>> @process
... def gen_example(chan1, chan2):
...     gen = Alt(chan1, chan2) * 2
...     print gen.next()
...     print gen.next()
... 
>>> c1, c2 = Channel(), Channel()
>>> Par(send_msg(c1, 'yes'), send_msg(c2, 'no'), gen_example(c1, c2)).start()
yes
no
>>> 
    """

    def __init__(self, *args):
        super(Alt, self).__init__()
        for arg in args:
            assert isinstance(arg, Guard)
        self.guards = list(args)
        self.last_selected = None

    def poison(self):
        """Poison the last selected guard and unlink from the guard list.

        Sets self.last_selected to None.
        """
        _debug(str(type(self.last_selected)))
        self.last_selected.disable() # Just in case
        try:
            self.last_selected.poison()
        except Exception:
            pass
        _debug('Poisoned last selected.')
        self.guards.remove(self.last_selected)
        _debug('{0} guards'.format(len(self.guards)))
        self.last_selected = None

    def _preselect(self):
        """Check for special cases when any form of select() is called.

        If no object can be returned from a channel read and no
        exception is raised the return None. Any select() method
        should work like a Channel.read() which must always return a
        value if it does not throw an exception..
        """
        if len(self.guards) == 0:
            raise NoGuardInAlt()
        elif len(self.guards) == 1:
            _debug('Alt Selecting unique guard: {0}s'.format(self.guards[0].name))
            self.last_selected = self.guards[0]
            while not self.guards[0].is_selectable():
                self.guards[0].enable()
            return self.guards[0].select()
        return None

    def select(self):
        """Randomly select from ready guards."""
        if len(self.guards) < 2:
            return self._preselect()
        ready = []
        while len(ready) == 0:
            for guard in self.guards:
                guard.enable()
                _debug('Alt enabled all guards')
            time.sleep(0.01) # Not sure about this.
            ready = [guard for guard in self.guards if guard.is_selectable()]
            _debug('Alt got {0} items to choose from out of {1}'.format(len(ready), len(self.guards)))
        selected = _RANGEN.choice(ready)
        self.last_selected = selected
        for guard in self.guards:
            if guard is not selected:
                guard.disable()
        return selected.select()

    def fair_select(self):
        """Select a guard to synchronise with. Do not select the
        previously selected guard (unless it is the only guard
        available).
        """
        if len(self.guards) < 2:
            return self._preselect()
        ready = []
        while len(ready) == 0:
            for guard in self.guards:
                guard.enable()
                _debug('Alt enabled all guards')
            time.sleep(0.1) # Not sure about this.
            ready = [guard for guard in self.guards if guard.is_selectable()]
            _debug('Alt got {0} items to choose from, out of {1}'.format(len(ready), len(self.guards)))
        selected = None
        if self.last_selected in ready and len(ready) > 1:
            ready.remove(self.last_selected)
            _debug('Alt removed last selected from ready list')
        selected = _RANGEN.choice(ready)
        self.last_selected = selected
        for guard in self.guards:
            if guard is not selected:
                guard.disable()
        return selected.select()

    def pri_select(self):
        """Select a guard to synchronise with, in order of
        "priority". The guard with the lowest index in the L{guards}
        list has the highest priority.
        """
        if len(self.guards) < 2:
            return self._preselect()
        ready = []
        while len(ready) == 0:
            for guard in self.guards:
                guard.enable()
                _debug('Alt enabled all guards')
            time.sleep(0.01) # Not sure about this.
            ready = [guard for guard in self.guards if guard.is_selectable()]
            _debug('Alt got {0} items to choose from, out of {1}'.format(len(ready), len(self.guards)))
        self.last_selected = ready[0]
        for guard in ready[1:]:
            guard.disable()
        return ready[0].select()

    def __mul__(self, n):
        assert n > 0
        for _ in range(n):
            yield self.select()

    def __rmul__(self, n):
        assert n > 0
        for _ in range(n):
            yield self.select()


class Par(Process, _CSPOpMixin):
    """Run CSP processes in parallel.

    There are two ways to run processes in parallel.  Firstly, given
    two (or more) processes you can parallelize them with the //
    operator, like this:

>>> @process
... def foo(n):
...     print 'n:', n
... 
>>> foo(1) // (foo(2), foo(3))
n: 2
n: 1
n: 3
<Par(Par-5, initial)>
>>> 

    Notice that the // operator takes a CSPProcess on the left hand side
    and a sequence of processes on the right hand side.

    Alternatively, you can create a Par object which is a sort of CSP
    process and start that process manually:

>>> p = Par(foo(100), foo(200), foo(300))
>>> p.start()
n: 100
n: 300
n: 200
>>>     
    """

    def __init__(self, *procs, **kwargs):
        super(Par, self).__init__(None)
        self.procs = []
        for proc in procs:
            # FIXME: only catches shallow nesting.
            if isinstance(proc, Par):
                self.procs += proc.procs
            else:
                self.procs.append(proc)
        for proc in self.procs:
            proc.enclosing = self
        _debug('{0} processes in Par:'.format(len(self.procs)))

    def __ifloordiv__(self, proclist):
        """
        Run this Par in parallel with a list of others.
        """
        assert hasattr(proclist, '__iter__')
        self.procs = []
        for proc in proclist:
            # FIXME: only catches shallow nesting.
            if isinstance(proc, Par):
                self.procs += proc.procs
            else:
                self.procs.append(proc)
        for proc in self.procs:
            proc.enclosing = self
        _debug('{0} processes added to Par by //:'.format(len(self.procs)))
        self.start()

    def __str__(self):
        return 'CSP Par running in process {0}'.format(self.getPid())

    def terminate(self):
        """Terminate the execution of this process.
        FIXME: Should not be recursive. Is this ever called?!
        """
        for proc in self.procs:
            proc.terminate()
        if self._started:
            self.terminate()

    def wait(self):
        for proc in self.procs:
            if proc._started:
                proc.wait()

    def start(self):
        """Start then synchronize with the execution of parallel processes.
        Return when all parallel processes have returned.
        """
        try:
            for proc in self.procs:
                proc.start()
            for proc in self.procs:
                proc.wait()
        except ChannelPoison:
            _debug('{0}s got ChannelPoison exception in {1}'.format(str(self), self.getPid()))
            self.referent_visitor(self._args + tuple(self._kwargs.values()))
#            if self._started is not None: self.terminate()
        except KeyboardInterrupt:
            sys.exit()
        except Exception:
            typ, excn, tback = sys.exc_info()
            sys.excepthook(typ, excn, tback)

    def __len__(self):
        return len(self.procs)

    def __getitem__(self, index):
        """Can raise an IndexError if index is not a valid index of
        self.procs.
        """
        return self.procs[index]

    def __setitem__(self, index, value):
        assert isinstance(value, CSPProcess)
        self.procs[index] = value

    def __contains__(self, proc):
        return proc in self.procs


class Seq(Process, _CSPOpMixin):
    """Run CSP processes sequentially.

    There are two ways to run processes in sequence.  Firstly, given
    two (or more) processes you can sequence them with the > operator,
    like this:

>>> @process
... def foo(n):
...     print 'n:', n
... 
>>> foo(1) > foo(2) > foo(3)
n: 1
n: 2
n: 3
<Seq(Seq-14, initial)>
>>> 

    Secondly, you can create a Seq object which is a sort of CSP
    process and start that process manually:

>>> s = Seq(foo(100), foo(200), foo(300))
>>> s.start()
n: 100
n: 200
n: 300
>>>
    """

    def __init__(self, *procs):
        super(Seq, self).__init__()
        self.procs = []
        for proc in procs:
            # FIXME: only catches shallow nesting.
            if isinstance(proc, Seq):
                self.procs += proc.procs
            else:
                self.procs.append(proc)
        for proc in self.procs:
            proc.enclosing = self
        return

    def __str__(self):
        return 'CSP Seq running in process {0}.'.format(self.getPid())

    def start(self):
        """Start this process running.
        """
        try:
            for proc in self.procs:
                proc.start()
                proc.wait()
        except ChannelPoison:
            _debug('{0} got ChannelPoison exception in {1}'.format(str(self), self.getPid()))
            self.referent_visitor(self._args + tuple(self._kwargs.values()))
            if self._started: self.terminate()
        except KeyboardInterrupt:
            sys.exit()
        except Exception:
            typ, excn, tback = sys.exc_info()
            sys.excepthook(typ, excn, tback)


### Guards and channels

class Guard(object):
    """Abstract class to represent CSP guards.

    All methods must be overridden in subclasses.
    """

    def is_selectable(self):
        """Should return C{True} if this guard can be selected by an L{Alt}.
        """
        raise NotImplementedError('Must be implemented in subclass')

    def enable(self):
        """Prepare for, but do not commit to a synchronisation.
        """
        raise NotImplementedError('Must be implemented in subclass')

    def disable(self):
        """Roll back from an L{enable} call.
        """
        raise NotImplementedError('Must be implemented in subclass')

    def select(self):
        """Commit to a synchronisation started by L{enable}.
        """
        raise NotImplementedError('Must be implemented in subclass')

    def poison(self):
        """Terminate all processes attached to this guard.
        """
        pass

    def __str__(self):
        return 'CSP Guard: must be subclassed.'

    def __or__(self, other):
        assert isinstance(other, Guard)
        return Alt(self, other).select()

    def __ror__(self, other):
        assert isinstance(other, Guard)
        return Alt(self, other).select()
    
    
class Channel(Guard):
    """CSP Channel objects.

    In python-csp there are two sorts of channel. In JCSP terms these
    are Any2Any, Alting channels. However, each channel creates an
    operating system level pipe. Since this is a file object the
    number of channels a program can create is limited to the maximum
    number of files the operating system allows to be open at any one
    time. To avoid this bottleneck use L{FileChannel} objects, which
    close the file descriptor used for IPC after every read or write
    operations. Read and write operations are, however, over 20 time
    slower when performed on L{FileChannel} objects.

    Subclasses of C{Channel} must call L{_setup()} in their
    constructor and override L{put}, L{get}, L{__del__}.

    A CSP channel can be created with the Channel class:

>>> c = Channel()
>>>

    Each Channel object should have a unique name in the network:

>>> print c.name
1ca98e40-5558-11df-8e5b-002421449824
>>> 

    The Channel can then be passed as an argument to any CSP process
    and then be used either to read (using the .read() method) or to
    write (using the .write() method). For example:

>>> @process
... def send(cout, data):
...     cout.write(data)
... 
>>> @process
... def recv(cin):
...     print 'Got:', cin.read()
... 
>>> c = Channel()
>>> send(c, 100) // (recv(c),)
Got: 100
<Par(Par-3, initial)>
>>> 
    """

    def __init__(self):
        self.name = str(uuid.uuid1())
        self._wlock = None     # Write lock protects from races between writers.
        self._rlock = None     # Read lock protects from races between readers.
        self._available = None     # Released if writer has made data available.
        self._taken = None         # Released if reader has taken data.
        self._is_alting = None     # True if engaged in an Alt synchronisation.
        self._is_selectable = None # True if can be selected by an Alt.
        self._memstore = SharedMemory(self.name)
        self._poisoned = None
        self._setup()
        super(Channel, self).__init__()
        _debug('Channel created: {0}'.format(self.name))

    def _setup(self):
        """Set up synchronisation.

        MUST be called in __init__ of this class and all subclasses.
        """
        # TODO: Move into __init__
        # Process-safe synchronisation.
        self._wlock = Lock(self.name + '_wlock')    # Write lock.
        self._rlock = Lock(self.name + '_rlock')    # Read lock.
        self._available = Semaphore(self.name + '_available')
        self._taken     = Semaphore(self.name + '_taken')
        # Process-safe synchronisation for CSP Select / Occam Alt.
        self._is_alting = Value(self.name + '_is_alting', False, ty=bool)
        self._is_selectable = Value(self.name + '_is_selectable', False, ty=bool)
        # Is this channel poisoned?
        self._poisoned = Value(self.name + '_poisoned', False, ty=bool)
        return

    def __getstate__(self):
        """Called when this channel is pickled, this makes the channel mobile.
        """
        newdict = self.__dict__.copy()
        del newdict['_memstore']
        del newdict['_available']
        del newdict['_taken']
        del newdict['_wlock']
        del newdict['_rlock']
        del newdict['_is_alting']  
        del newdict['_is_selectable']
        del newdict['_poisoned']
        return newdict

    def __setstate__(self, newdict):
        """Called when this channel is unpickled, this makes the channel mobile.
        """
        newdict['_memstore'] = SharedMemory(newdict['name'], create=False)
        newdict['_wlock'] = Lock(newdict['name'] + '_wlock', create=False)
        newdict['_rlock'] = Lock(newdict['name'] + '_rlock', create=False)
        newdict['_available'] = Semaphore(newdict['name'] + '_available', create=False)
        newdict['_taken']     = Semaphore(newdict['name'] + '_taken', create=False)
        newdict['_is_alting'] = Value(newdict['name'] + '_is_alting', create=False)
        newdict['_is_selectable'] = Value(newdict['name'] + '_is_selectable', False, create=False)
        newdict['_poisoned'] = Value(newdict['name'] + '_poisoned', create=False)
        self.__dict__.update(newdict)
        return
    
    def put(self, item):
        """Put C{item} on a process-safe store.
        """
        self.checkpoison()
        self._memstore.put(item)
        return

    def get(self):
        """Get a Python object from a process-safe store.
        """
        self.checkpoison()
        return self._memstore.get()

    def is_selectable(self):
        """Test whether Alt can select this channel.
        """
        _debug('Alt THINKS _is_selectable IS: {0}'.format(str(self._is_selectable.get())))
        self.checkpoison()
        return self._is_selectable.get()

    def write(self, obj):
        """Write a Python object to this channel.
        """
        self.checkpoison()
        _debug('+++ Write on Channel {0} started.'.format(self.name))
        with self._wlock: # Protect from races between multiple writers.
            # Make the object available to the reader.
            self.put(obj)
            # Announce the object has been released to the reader.
            self._available.release()
            _debug('++++ Writer on Channel {0}: _available: {1} _taken: {2}. '.format(self.name, self._available.value(), self._taken.value()))
            # Block until the object has been read.
            self._taken.acquire()
            # Remove the object from the channel.
        _debug('+++ Write on Channel {0} finished.'.format(self.name))

    def read(self):
        """Read (and return) a Python object from this channel.
        """
        assert not self._is_alting.get()
        assert not self._is_selectable.get()
        self.checkpoison()
        _debug('+++ Read on Channel {0} started.'.format(self.name))
        with self._rlock: # Protect from races between multiple readers.
            # Block until an item is in the Channel.
            _debug('++++ Reader on Channel {0}: _available: {1} _taken: {2}. '.format(self.name, self._available.value(), self._taken.value()))
            self._available.acquire()
            # Get the item.
            obj = self.get()
            # Announce the item has been read.
            self._taken.release()
        _debug('+++ Read on Channel {0} finished.'.format(self.name))
        return obj

    def enable(self):
        """Enable a read for an Alt select.

        MUST be called before L{select()} or L{is_selectable()}.
        """
        self.checkpoison()
        # Prevent re-synchronization.
        if self._is_selectable.get():
            # Be explicit.
            return None
        self._is_alting.set(True)
        with self._rlock:
            # Attempt to acquire _available.
            time.sleep(0.00001) # Won't work without this -- why?
            try:
                self._available.acquire(0)
                self._is_selectable.set(True)
            except SemNotAvailable:
                self._is_selectable.set(False)
        _debug('Enable on guard {0} _is_selectable: {1} _available: {2}'.format(self.name, str(self._is_selectable.get()), str(self._available.value())))

    def disable(self):
        """Disable this channel for Alt selection.

        MUST be called after L{enable} if this channel is not selected.
        """
        self.checkpoison()
        self._is_alting.set(False)
        if self._is_selectable.get():
            with self._rlock:
                self._available.release()
            self._is_selectable.set(False)

    def select(self):
        """Complete a Channel read for an Alt select.
        """
        self.checkpoison()
        _debug('channel select starting')
        assert self._is_selectable.get()
        with self._rlock:
            _debug('got read lock on channel {0} _available: {1}'.format(self.name, str(self._available.value())))
            # Obtain object on Channel.
            obj = self.get()
            _debug('got obj')
            # Notify write() that object is taken.
            self._taken.release()
            _debug('released _taken')
            # Reset flags to ensure a future read / enable / select.
            self._is_selectable.set(False)
            self._is_alting.set(False)
            _debug('reset bools')
        if obj == _POISON:
            self.poison()
            raise ChannelPoison()
        return obj

    def __str__(self):
        return 'Channel using OS pipe for IPC.'

    def checkpoison(self):
        if self._poisoned.get():
            _debug('{0} is poisoned. Raising ChannelPoison()'.format(self.name))
            raise ChannelPoison()

    def poison(self):
        """Poison a channel causing all processes using it to terminate.

        A set of communicating processes can be terminated by
        "poisoning" any of the channels used by those processes. This
        can be achieved by calling the poison() method on any
        channel. For example:

>>> @process
... def send5(cout):
...     for i in xrange(5):
...             print 'send5 sending:', i
...             cout.write(i)
...             time.sleep(random.random() * 5)
...     return
... 
>>> @process
... def recv(cin):
...     for i in xrange(5):
...             data = cin.read()
...             print 'recv got:', data
...             time.sleep(random.random() * 5)
...     return
... 
>>> @process
... def interrupt(chan):
...     time.sleep(random.random() * 7)
...     print 'Poisoning channel:', chan.name
...     chan.poison()
...     return
... 
>>> doomed = Channel()
>>> send(doomed) // (recv(doomed), poison(doomed))
send5 sending: 0
recv got: 0
send5 sending: 1
recv got: 1
send5 sending: 2
recv got: 2
send5 sending: 3
recv got: 3
send5 sending: 4
recv got: 4
Poisoning channel: 5c906e38-5559-11df-8503-002421449824
<Par(Par-5, initial)>
>>> 
        """
        self._poisoned.set(True)
        # Avoid race conditions on any waiting readers / writers.
        self._available.release() 
        self._taken.release()


### Function decorators

def process(func):
    """Decorator to turn a function into a CSP process.

    There are two ways to create a new CSP process. Firstly, you can
    use the @process decorator to convert a function definition into a
    CSP Process. Once the function has been defined, calling it will
    return a new CSPProcess object which can be started manually, or
    used in an expression:

>>> @process
... def foo(n):
...     print 'n:', n
... 
>>> foo(100).start()
>>> n: 100

>>> foo(10) // (foo(20),)
n: 10
n: 20
<Par(Par-5, initial)>
>>> 

    Alternatively, you can create a CSPProcess object directly and pass a
    function (and its arguments) to the CSPProcess constructor:

>>> def foo(n):
...     print 'n:', n
... 
>>> p = CSPProcess(foo, 100)
>>> p.start()
>>> n: 100

>>> 
    """
    @wraps(func)
    def _call(*args, **kwargs):
        """Call the target function."""
        return CSPProcess(func, *args, **kwargs)
    return _call


def forever(func):
    """Decorator to turn a function into a CSP server process.

    It is preferable to use this rather than @process, to enable the
    CSP tracer to terminate correctly and produce a CSP model, or
    other debugging information.

    A server process is one which runs in an infinite loop. You can
    create a "normal" process which runs in an infinite loop, but by
    using server processes you allow the python-csp debugger to
    correctly generate information about your programs.

    There are two ways to create a new CSP server process. Firstly,
    you can use the @forever decorator to convert a generator into a
    CSPServer object. Once the function has been defined, calling it
    will return a new CSPServer object which can be started manually,
    or used in an expression:

>>> @forever
... def integers():
...     n = 0
...     while True:
...             print n
...             n += 1
...             yield
... 
>>> integers().start()
>>> 0
1
2
3
4
5
...
KeyboardInterrupt
>>>

    Alternatively, you can create a CSPServer object directly and pass a
    function (and its arguments) to the CSPServer constructor:

>>> def integers():
...     n = 0
...     while True:
...             print n
...             n += 1
...             yield
... 
>>> i = CSPServer(integers)
>>> i.start()
>>> 0
1
2
3
4
5
...
KeyboardInterrupt    
    """
    @wraps(func)
    def _call(*args, **kwargs):
        """Call the target function."""
        return CSPServer(func, *args, **kwargs)
    return _call


### List of CSP based types (class names). Used by _is_csp_type.
_CSPTYPES = [CSPProcess, Par, Seq, Alt]


def _is_csp_type(obj):
    """Return True if obj is any type of CSP process."""
    return isinstance(obj, tuple(_CSPTYPES))


def _nop():
    pass


class Skip(CSPProcess, Guard):
    """Guard which will always return C{True}. Useful in L{Alt}s where
    the programmer wants to ensure that L{Alt.select} will always
    synchronise with at least one guard.

    Skip is a built in guard type that can be used with Alt
    objects. Skip() is a default guard which is always ready and has
    no effect. This is useful where you have a loop which calls
    select(), pri_select() or fair_select() on an Alt object
    repeatedly and you do not wish the select statement to block
    waiting for a channel write, or other synchronisation. The
    following is a trivial example of an Alt which uses Skip():

>>> alt = Alt(Skip())
>>> for i in xrange(5):
...     print alt.select()
... 
Skip
Skip
Skip
Skip
Skip
>>> 

    Where you have an Alt() object which mixes Skip() with other guard
    types, be sure to complete all necessary channel reads or other
    synchronisations, otherwise your code will hang.
    """

    def __init__(self):
        Guard.__init__(self)
        CSPProcess.__init__(self, _nop)
        self.name = '__Skip__'

    def is_selectable(self):
        """Skip is always selectable."""
        return True

    def enable(self):
        """Has no effect."""
        pass

    def disable(self):
        """Has no effect."""
        pass

    def select(self):
        """Has no effect."""
        return 'Skip'

    def __str__(self):
        return 'Skip guard is always selectable / process does nothing.'

