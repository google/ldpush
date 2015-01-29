#!/usr/bin/python
#
# Copyright 2014 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Fake classes for unit tests.

The class FakeSshClient is a fake for paramiko.SSHClient, it implements a very
minimal set of methods just enough too stub out paramiko.SSHClient when used in
unit test for clients based on pexpect_client.ParamikoSshConnection.
The classes FakeChannel and FakeTransport are substitutes for their paramiko
counterparts Channel and Transport.
"""
import re
import socket
import time


# pylint: disable=g-bad-name
class Error(Exception):
  pass


class FakeChannelError(Error):
  """An error occured in the fake Channel class."""


class FakeTransport(object):
  """A fake transport class for unit test purposes."""

  def __init__(self):
    self.active = True

  def is_active(self):
    return self.active


class FakeChannel(object):
  """A fake channel class for unit test purposes."""

  def __init__(self, command_response_dict, exact=True):
    """Initialize FakeChannel.

    Args:
      command_response_dict: A dict, where if d[sent] defines how to respond
        to sent. d[sent] can be either a str, a list of str or a callable
        which will be called to create the response. If the response
        is a list, each entry is returned on this and subsequent calls of recv.
      exact: a bool, If True, treat sent as a string rather than a regexp.
    """
    self.command_responses = []
    for receive_re, send_gen in command_response_dict.iteritems():
      if exact:
        receive_re = re.escape(receive_re)
      if not callable(send_gen):
        # send_gen() = send_gen
        send_gen = (lambda(s): lambda: s)(send_gen)
      self.command_responses.append((re.compile(receive_re), send_gen))
    self.transport = FakeTransport()
    self.timeout = None
    self.last_sent = '__logged_in__'
    self.sent = []
    self.extras = []

  def set_combine_stderr(self, unused_arg):
    pass

  def get_id(self):
    return 1

  def get_transport(self):
    return self.transport

  def settimeout(self, timeout):
    self.timeout = timeout

  def recv(self, unused_size):
    """Respond to what was last sent."""
    if self.extras:
      return self.extras.pop(0)
    if self.last_sent is not None:
      last_sent = self.last_sent
      self.last_sent = None
      for pattern, response in self.command_responses:
        if pattern.match(last_sent):
          responses = response()
          if isinstance(responses, list):
            self.extras = responses[1:]
            return responses[0]
          return responses
      raise FakeChannelError('unknown input %r' % last_sent)
    time.sleep(self.timeout)
    raise socket.timeout('fake timeout')

  def send(self, command):
    self.last_sent = command
    self.sent.append(command)


class FakeSshClient(object):
  """A fake SSH client class for unit test purposes."""

  def __init__(self, command_response_dict, exact=True):
    """Initialises a FakeSshClient.

    Args:
      command_response_dict: A dict, where the values are either strings
        or parameter-free callables returning a string and the keys
        are regular expressions.
      exact: A boolean, If True, the keys above are plain strings.

    A fake ssh that matches sent data defined by the keys
    in command_response_dict and responds with the corresponding string value.
    """

    self.channel = FakeChannel(command_response_dict, exact)

  def Connect(self, **unused_kwargs):
    return self

  def invoke_shell(self):
    return self.channel
