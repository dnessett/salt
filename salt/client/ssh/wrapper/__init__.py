"""
The ssh client wrapper system contains the routines that are used to alter
how executions are run in the salt-ssh system, this allows for state routines
to be easily rewritten to execute in a way that makes them do the same tasks
as ZeroMQ salt, but via ssh.
"""

import logging
from collections.abc import MutableMapping

import salt.client.ssh
import salt.loader
import salt.utils.data
import salt.utils.json
from salt.defaults import NOT_SET
from salt.exceptions import CommandExecutionError, SaltException

log = logging.getLogger(__name__)


class SSHException(SaltException):
    """
    Indicates general command failure via salt-ssh.
    """

    _error = ""

    def __init__(
        self, stdout, stderr, retcode, result=NOT_SET, parsed=None, *args, **kwargs
    ):
        super().__init__(stderr, *args, **kwargs)
        self.stdout = stdout
        self.stderr = self._filter_stderr(stderr)
        self.result = result
        self.parsed = parsed
        self.retcode = retcode
        if args:
            self._error = args.pop(0)
        super().__init__(self._error)

    def _filter_stderr(self, stderr):
        stderr_lines = []
        skip_next = False
        for line in stderr.splitlines():
            if skip_next:
                skip_next = False
                continue
            # Filter out deprecation warnings from stderr to the best of
            # our ability since they are irrelevant to the command output and cause noise.
            parts = line.split(":")
            if len(parts) > 2 and "DeprecationWarning" in parts[2]:
                # DeprecationWarnings print two lines, the second one being the
                # line that caused the warning.
                skip_next = True
                continue
            stderr_lines.append(line)
        return "\n".join(stderr_lines)

    def to_ret(self):
        ret = {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "retcode": self.retcode,
            "parsed": self.parsed,
        }
        if self._error:
            ret["_error"] = self._error
        if self.result is not NOT_SET:
            ret["return"] = self.result
        return ret


class SSHCommandExecutionError(SSHException, CommandExecutionError):
    """
    Thrown whenever a non-zero exit code is returned.
    This was introduced to make the salt-ssh FunctionWrapper behave
    more like the usual one, in particular to force template rendering
    to stop when a function call results in an exception.
    """

    _error = "The command resulted in a non-zero exit code"

    def to_ret(self):
        if self.parsed and "local" in self.parsed:
            # Wrapped commands that indicate a non-zero retcode
            return self.parsed["local"]
        return super().to_ret()

    def __str__(self):
        if self.retcode > 0:
            return f"{self._error}: {self.stderr or self.stdout}"
        return self._error


class SSHPermissionDeniedError(SSHException):
    """
    Thrown when "Permission denied" is found in stderr
    """

    _error = "Permission denied"


class SSHReturnDecodeError(SSHException):
    """
    Thrown when JSON-decoding stdout fails and the retcode is 0 otherwise
    """

    _error = "Failed to return clean data"


class SSHMalformedReturnError(SSHException):
    """
    Thrown when a decoded return dict is not formed as
    {"local": {"return": ...}}
    """

    _error = "Return dict was malformed"


class LoadedMod:
    """
    This class is used as a proxy to a loaded wrapper module
    or the module part of a call to the target when
    a non-recommended syntax is used for loader access
    (like ``salt.grains.get`` or ``salt["grains"].get``).
    """

    __slots__ = ("mod", "wrapper")

    def __init__(self, mod, wrapper):
        self.mod = mod
        self.wrapper = wrapper

    def __getattr__(self, name):
        """
        Return the requested function.
        """
        try:
            return self.wrapper[f"{self.mod}.{name}"]
        except KeyError:
            # This  shouldn't happen since we wrap unknown calls to the target
            raise AttributeError(
                f"No attribute by the name of {name} was found on {self.mod}"
            )

    def __setitem__(self, name, value):
        """
        Set aliases for functions
        """
        self.wrapper[f"{self.mod}.{name}"] = value

    def __delitem__(self, name):
        """
        Remove aliases for functions
        """
        del self.wrapper[f"{self.mod}.{name}"]

    def __repr__(self):
        try:
            # Determine if we're representing a wrapper module or
            # an unknown execution module on the target.
            # We need to use the attribute since __getitem__ does not
            # allow module-level access.
            getattr(
                self.wrapper.wfuncs, self.mod
            )  # pylint: disable=pointless-statement
            prefix = self.wrapper.wfuncs.loaded_base_name + "."
            name = self.__class__.__name__
        except AttributeError:
            prefix = ""
            name = "SSHTargetMod"
        return f"<{name} module='{prefix}{self.mod}'>"


class FunctionWrapper(MutableMapping):
    """
    Create an object that acts like the salt function dict and makes function
    calls remotely via the SSH shell system
    """

    def __init__(
        self,
        opts,
        id_,
        host,
        wfuncs=None,
        mods=None,
        fsclient=None,
        aliases=None,
        minion_opts=None,
        **kwargs,
    ):
        super().__init__()
        self.wfuncs = wfuncs if wfuncs is not None else {}
        self.opts = opts
        self.mods = mods if isinstance(mods, dict) else {}
        self.kwargs = {"id_": id_, "host": host}
        self.fsclient = fsclient
        self.kwargs.update(kwargs)
        self.aliases = aliases
        if self.aliases is None:
            self.aliases = {}
        self.minion_opts = minion_opts

    def __contains__(self, key):
        """
        We need to implement a __contains__ method, othwerwise when someone
        does a contains comparison python assumes this is a sequence, and does
        __getitem__ keys 0 and up until IndexError
        """
        try:
            self[key]  # pylint: disable=pointless-statement
            return True
        except KeyError:
            return False

    def __getitem__(self, cmd):
        """
        Return the function call to simulate the salt local lookup system
        """
        if "." not in cmd:
            # Form of salt.cmd.run in Jinja -- it's expecting a subdictionary
            # containing only 'cmd' module calls
            # We don't know which modules are available on the target, so just
            # return the module namespace without any checks.
            return LoadedMod(cmd, self)

        if cmd in self.wfuncs:
            return self.wfuncs[cmd]

        if cmd in self.aliases:
            return self.aliases[cmd]

        def caller(*args, **kwargs):
            """
            The remote execution function
            """
            argv = [cmd]
            argv.extend([salt.utils.json.dumps(arg) for arg in args])
            argv.extend(
                [
                    "{}={}".format(
                        salt.utils.stringutils.to_str(key), salt.utils.json.dumps(val)
                    )
                    for key, val in kwargs.items()
                ]
            )
            single = salt.client.ssh.Single(
                self.opts,
                argv,
                mods=self.mods,
                disable_wipe=True,
                fsclient=self.fsclient,
                minion_opts=self.minion_opts,
                **self.kwargs,
            )
            stdout, stderr, retcode = single.cmd_block()
            return parse_ret(stdout, stderr, retcode, result_only=True)

        return caller

    def __setitem__(self, cmd, value):
        """
        Set aliases for functions
        """
        if "." not in cmd:
            # Form of salt.cmd.run in Jinja -- it's expecting a subdictionary
            # containing only 'cmd' module calls, in that case. We don't
            # support assigning directly to prefixes in this way
            raise KeyError(f"Cannot assign to module key {cmd} in the FunctionWrapper")

        if cmd in self.wfuncs:
            self.wfuncs[cmd] = value

        # Here was assume `value` is a `caller` function from __getitem__.
        # We save it as an alias and then can return it when referenced
        # later in __getitem__
        self.aliases[cmd] = value

    def __delitem__(self, cmd):
        """
        Remove aliases for functions
        """
        if "." not in cmd:
            # Form of salt.cmd.run in Jinja
            raise KeyError(f"Cannot delete module key {cmd} in the FunctionWrapper")

        if cmd in self.wfuncs:
            del self.wfuncs[cmd]

        del self.aliases[cmd]

    def __len__(self):
        """
        Return the count of wrapper modules and aliases.
        We don't know which modules will be available on the target.
        """
        return len(self.wfuncs) + len(self.aliases)

    def __iter__(self):
        """
        Iterate through wrapper modules and aliases.
        We don't know which modules will be available on the target.
        """
        yield from self.wfuncs
        yield from self.aliases

    def __getattr__(self, mod_or_func):
        """
        Ensure the behavior is similar to the usual LazyLoader regarding
        attribute access.
        """
        if mod_or_func.startswith("__") and mod_or_func.endswith("__"):
            # Don't pretend dunders are set.
            raise AttributeError(mod_or_func)
        try:
            return self.__getitem__(mod_or_func)
        except KeyError:
            raise AttributeError(mod_or_func)


def parse_ret(stdout, stderr, retcode, result_only=False):
    """
    Parse the output of a remote or local command and return its
    result. Raise exceptions if the command has a non-zero exitcode
    or its output is not valid JSON or is not in the expected format,
    usually ``{"local": {"return": value}}`` (+ optional keys in the "local" dict).
    """
    try:
        retcode = int(retcode)
    except (TypeError, ValueError):
        log.warning("Got an invalid retcode for host: '%s'", retcode)
        retcode = 1

    if "Permission denied" in stderr:
        # -failed to upload file- is detecting scp errors
        # Errors to ignore when Permission denied is in the stderr. For example
        # scp can get a permission denied on the target host, but they where
        # able to accurate authenticate against the box
        ignore_err = ["failed to upload file"]
        check_err = [x for x in ignore_err if stderr.count(x)]
        if not check_err:
            raise SSHPermissionDeniedError(
                stdout=stdout, stderr=stderr, retcode=retcode
            )

    result = NOT_SET
    error = None
    data = None

    try:
        data = salt.utils.json.find_json(stdout)
    except ValueError:
        # No valid JSON output was found
        error = SSHReturnDecodeError
    else:
        if isinstance(data, dict) and len(data) < 2 and "local" in data:
            result = data["local"]
            try:
                remote_retcode = result["retcode"]
            except (KeyError, TypeError):
                pass
            else:
                try:
                    # Ensure a reported local retcode is kept (at least)
                    retcode = max(retcode, remote_retcode)
                except (TypeError, ValueError):
                    log.warning(
                        "Host reported an invalid retcode: '%s'", remote_retcode
                    )
                    retcode = max(retcode, 1)

            if not isinstance(result, dict):
                # When a command has failed, the return is dumped as-is
                # without declaring it as a result, usually a string or list.
                error = SSHCommandExecutionError
            elif result_only:
                try:
                    result = result["return"]
                except KeyError:
                    error = SSHMalformedReturnError
                    result = NOT_SET
        else:
            error = SSHMalformedReturnError

    if retcode:
        error = SSHCommandExecutionError
    if error is not None:
        raise error(
            stdout=stdout,
            stderr=stderr,
            retcode=retcode,
            result=result,
            parsed=data,
        )
    return result
