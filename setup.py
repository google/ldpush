from cx_Freeze import setup, Executable

# Dependencies are automatically detected, but it might need
# fine tuning.
buildOptions = dict(packages = [], excludes = [])

executables = [
    Executable('push.py', 'Console', targetName = 'ldpush')
]

setup(name='ldpush',
      version = '1.0',
      description = 'A cross-vendor network configuration distribution tool. This is useful for pushing ACLs or other pieces of configuration to network elements. It can also be used to send commands to a list of devices and gather the results.',
      options = dict(build_exe = buildOptions),
      executables = executables)
