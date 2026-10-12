# Add your own setup

## Marine

![Crayon drawing of a laptop preparing a memory card for a radio.](../assets/flash-crayon.png)

### Give it extra jobs after setup

The person making your radio can pack little setup instructions into it.
The radio does those jobs after its normal setup finishes.

An extra job can fail while the radio still works. Check the job's result.

## User

### What a setup script is for

An operator can include extra instructions when flashing, for example to
install an organization's settings or authorized login keys. These are
called setup scripts. They run on the radio after normal MANET setup.
You do not need to create any to use the standard radio.

Place approved scripts in the flasher's `additional-scripts/` directory.
The flasher validates and embeds them before writing the image. Names such
as `10-site-settings.sh` and `20-extra-service.sh` establish their execution
order. Each script needs an interpreter line at its start.

### Know whether the extra work finished

The SSH login banner reports the setup-script tally and names failures.
Their detailed output is in `/var/log/manet-user-scripts.log`. A failed
extra script does not turn a successful MANET provisioning verdict into a
failure, because the mesh may be working normally.

If power interrupts the set, the next boot resumes unfinished work without
repeating completed scripts. A script that finishes with an error is recorded
as completed and is not automatically retried. Its author should investigate
before forcing the set to run again, since repeated actions may have side
effects. Scripts receive five minutes each by default.

The embedded first-boot file is readable on the boot partition and remains
there after setup. Follow the [script reference's handling of secrets](../../../MANET/provisioning/additional-scripts/README.md#do-not-put-secrets-in-here)
when preparing site-specific credentials.

## Developer

### Validation precedes writing an image

Linux and Windows flashers accept a flat set of named text scripts. They
normalize supported encodings, byte-order marks, CRLF and a missing final
newline on a copy, leaving the operator's file untouched. A missing shebang
is skipped; invalid names, empty data, NUL bytes and unsupported encoding
are errors. Shell/Python syntax checks run when the required parsers are
available. Other interpreters are reported without executing their code on
the flashing host.

The flashers embed quoted heredocs at the explicit additional-scripts marker
in each provisioning template. The marker must remain before the template's
final reboot; appending after it would produce a valid-looking image whose
hooks never run. Payloads above 256 KB warn and above 2 MB are refused. Large
or binary data should be retrieved by an appropriate runtime setup action.

### The runtime records completed attempts

Scripts arrive under `/var/lib/manet-user-scripts/`, run as root in lexical
order, and append output to the dedicated log. Completion state and the
whole-set marker are siblings of that directory, preventing state files from
being treated as executable inputs. The shebang and disabled/backup-name
filters run again for scripts copied onto a live node.

Each completed attempt records name, exit status and time. Interruption before
completion leaves work to resume; a recorded nonzero exit is still a finished
attempt and needs explicit retry. The runner's default timeout is 300 seconds,
overridable by `user_script_timeout` in mesh configuration. Forced execution
repeats the set, so authors should make actions safe to repeat where possible.

The runner always exits successfully to its caller and never writes the
provisioning verdict. Its own per-script records carry failures. This is an
intentional separation: core provisioning cannot become failed merely because
a site-specific step returned nonzero. A caller must inspect the hook results
instead of treating the runner's exit code as proof every script succeeded.

### Maintenance and packaging boundary

Operator hooks customize a deployment. General runtime changes belong in
the runtime source and package release, with their normal activation and
recovery design. A hook is not a substitute for a coordinated mesh-wide
configuration transaction, and forcing hooks during an update would violate
their completed-attempt semantics.

The [full script reference](../../../MANET/provisioning/additional-scripts/README.md)
contains accepted languages, filename rules, exact paths and retry commands.
[Flasher internals](../../../docs/provisioning-internals.md) explains token
substitution and embedding, while [first boot](first-boot.md) describes the
core provisioning lifecycle into which these hooks fit.
