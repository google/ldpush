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
"""Distribute bits of configuration to network elements.

Given some device names and configuration files (or a list of configuration
files with names hinting at the target device) send the configuration to the
target devices. These types of pushes can be IO bound, so threading is
appropriate.
"""

import getpass
import logging
import os
import Queue
import socket
import sys
import threading

import gflags
import progressbar

# Eval is used for building vendor objects.
# pylint: disable-msg=W0611
import aruba
import brocade
import cisconx
import ciscoxr
import hp
import ios
import junos
import paramiko_device
# pylint: enable-msg=W0611
import push_exceptions as exceptions


FLAGS = gflags.FLAGS

gflags.DEFINE_list('targets', '', 'A comma separated list of target devices.',
                     short_name='T')

gflags.DEFINE_bool('canary', False,
                   'Do everything possible, save for applying the config.',
                   short_name='c')

gflags.DEFINE_bool('devices_from_filenames', False,
                   'Use the configuration file names to determine the target '
                   'device.', short_name='d')

gflags.DEFINE_string('vendor', '', 'A vendor name. Must be one of the '
                     'implementations in this directory',
                     short_name='V')

gflags.DEFINE_string('user', '', 'Username for logging into the devices. This '
                     'will default to your own username.',
                     short_name='u')

gflags.DEFINE_string('command', '', 'Rather than a config file, you would like '
                     'to issue a command and get a response.',
                     short_name='C')

gflags.DEFINE_string('suffix', '', 'Append suffix onto each target provided.',
                     short_name='s')

gflags.DEFINE_integer('threads', 20, 'Number of push worker threads.',
                      short_name='t')

gflags.DEFINE_bool('verbose', False,
                   'Display full error messages.', short_name='v')


class Error(Exception):
  """Base exception class."""


class UsageError(Error):
  """Incorrect flags usage."""


class PushThread(threading.Thread):
  def __init__(
      self, task_queue, output_queue, error_queue, vendor_class, password):
    """Initiator.

    Args:
      task_queue: Queue.Queue holding two-tuples (str, str);
                  Resolvable device name or IP of the target,
                  configuration or command.
      output_queue: Queue.Queue holding two-tuples (str, str);
                    Resolvable device name or IP of the target,
                    output from push.
      error_queue: Queue.Queue holding two-tuples (str, str);
                   Resolvable device name or IP of the target,
                   error string from caught exception.
      vendor_class: type; Vendor appropriate class to use for this push.
      password: str; Password to use for devices (username is set in FLAGS).
    """
    threading.Thread.__init__(self)
    self._task_queue = task_queue
    self._output_queue = output_queue
    self._error_queue = error_queue
    self._vendor_class = vendor_class
    self._password = password


  def run(self):
    """Work on emptying the task queue."""
    while not self._task_queue.empty():
      target, command_or_config = self._task_queue.get()
      # This is a workaround. The base_device.BaseDevice class requires
      # loopback_ipv4 for ultimately passing on to sshclient.Connect - yet this
      # can be a hostname that resolves to a AAAA, kooky I know.
      # TODO(ryanshea): open bug for this.
      device = self._vendor_class(host=target, loopback_ipv4=target)

      # Connect.
      try:
        device.Connect(username=FLAGS.user, password=self._password)
      except exceptions.ConnectError as e:
        self._error_queue.put((target, e))
        continue

      # Send command or config.
      if FLAGS.command:
        response = device.Cmd(command=command_or_config)
        self._output_queue.put((target, response))
      else:
        try:
          response = device.SetConfig(
              destination_file='running-config', data=command_or_config,
              canary=FLAGS.canary)
        except exceptions.SetConfigError as e:
          self._error_queue.put((target, e))
        self._output_queue.put((target, response.transcript))

      device.Disconnect()


def JoinFiles(files):
  """Take a list of file names, read and join their content.

  Args:
    files: list; String filenames to open and read.
  Returns:
    str; The consolidated content of the provided filenames.
  """
  configlet = ''
  for f in files:
    # Let IOErrors happen naturally.
    configlet = configlet + (open(f).read())
  return configlet


def CheckFlags(files, class_path):
  """Validates flag sanity.

  Args:
    files: list; from argv[1:]
    class_path: str; class path of a vendor, for use by eval.
  Returns:
    type: A vendor class.
  Raises:
    UsageError: on flag mistakes.
  """
  # Flags "devices" and "devices_from_filenames" are mutually exclusive.
  if ((not FLAGS.targets and not FLAGS.devices_from_filenames)
      or (FLAGS.targets and FLAGS.devices_from_filenames)):
    raise UsageError(
        'No targets defined, try --targets.')

  # User must provide a vendor.
  elif not FLAGS.vendor:
    raise UsageError(
        'No vendor defined, try the --vendor flag (i.e. --vendor ios)')

  # We need some configuration files unless --command is used.
  elif not files and not FLAGS.command:
    raise UsageError(
        'No configuration files provided. Provide these via argv / glob.')
  # Ensure the provided vendor is implemented.
  else:
    try:
      pusher = eval(class_path)
    except NameError:
      raise UsageError(
          'The vendor "%s" is not implemented or imported. Please select a '
          'valid vendor' % FLAGS.vendor)
    return pusher


def main(argv):
  """Check flags and start the threaded push."""

  files = FLAGS(argv)[1:]

  # Vendor implementations must be named correctly, i.e. IosDevice.
  vendor_classname = FLAGS.vendor.lower().capitalize() + 'Device'
  class_path = '.'.join([FLAGS.vendor.lower(), vendor_classname])

  pusher = CheckFlags(files, class_path)

  if not FLAGS.user:
    FLAGS.user = getpass.getuser()

  # Queues will hold two tuples, (device_string, config) and
  # (device_string, output) respectively.
  task_queue = Queue.Queue()
  output_queue = Queue.Queue()
  # Holds target strings of devices with connection errors.
  error_queue = Queue.Queue()

  # files is a slight misnomer, this is meant to catch length of
  # targets, if true, otherwise devices_from_filenames files.
  targets_or_files = FLAGS.targets or files

  # Build the mapping of target to configuration.
  if FLAGS.devices_from_filenames:
    for device_file in files:
      # JoinFiles provides consistent file contents gathering.
      task_queue.put(
          (os.path.basename(device_file) + FLAGS.suffix,
           JoinFiles([device_file])))
    print 'Ready to push per-device configurations to %s' % targets_or_files
  else:
    print 'Ready to push %s to %s' % (files or FLAGS.command, FLAGS.targets)
    for device in FLAGS.targets:
      # Either the command string or consolidated config goes into the task
      # queue. The PushThread uses FLAGS.command to know if this is a command or
      # config to set.
      task_queue.put((device + FLAGS.suffix, FLAGS.command or JoinFiles(files)))

  passw = getpass.getpass('Password:')

  threads = []
  for _ in xrange(FLAGS.threads):
    worker = PushThread(task_queue, output_queue, error_queue, pusher, passw)
    threads.append(worker)
    worker.start()

  # Progress feedback.
  widgets = [
      'Pushing... ', progressbar.Percentage(), ' ',
      progressbar.Bar(marker=progressbar.RotatingMarker()), ' ',
      progressbar.ETA(), ' ', progressbar.FileTransferSpeed()]
  pbar = progressbar.ProgressBar(
      widgets=widgets, maxval=len(targets_or_files)).start()

  while not task_queue.empty():
    pbar.update(len(targets_or_files) - task_queue.qsize())
  pbar.finish()

  for worker in threads:
    worker.join()

  if FLAGS.command:
    while not output_queue.empty():
      dev, out = output_queue.get()
      print '#!# %s:%s #!#\n\n%s' % (dev, FLAGS.command, out)

  failures = []
  while not error_queue.empty():
    failures.append(error_queue.get())

  connect_fail = [
      (x, y) for (x, y) in failures if isinstance(y, exceptions.ConnectError)]
  config_fail = [
      (x, y) for (x, y) in failures if isinstance(y, exceptions.SetConfigError)]

  if connect_fail:
    print '\nFailed to connect to:\n%s\n' % ','.join(
        [x for x, _ in connect_fail])
    if FLAGS.verbose:
      for device, error in connect_fail:
        print '#!# %s:ConnectError #!#\n%s' % (device, error)
  if config_fail:
    print '\nSetting config failed:\n%s\n' % ','.join(
        [x for x, _ in config_fail])
    if FLAGS.verbose:
      for device, error in connect_fail:
        print '#!# %s:SetConfigError #!#\n%s' % (device, error)


if __name__ == '__main__':
  main(sys.argv)
