# Security policy

pysanoid runs as root and destroys ZFS snapshots. A bug that makes it destroy a snapshot the configuration means to
keep, or stop taking snapshots without saying so, can cost someone their data. Such a bug is treated as a security
problem here, whether or not it involves an attacker.

## What to report privately

- A configuration, pool state or sequence of runs that makes pysanoid destroy a snapshot its retention keeps.
- A case where pysanoid stops taking or pruning snapshots, or a unit fails, without a non-zero exit or a message.
- A way for someone other than root to make pysanoid run a command, read a file it shouldn't, or act on a dataset it
  isn't configured for, through its configuration, cache, lock or run files, or its hooks.

Anything else, such as a crash on a configuration pysanoid refuses anyway, or a difference from sanoid that loses
nothing, can go in an ordinary issue.

## How to report

Use GitHub's private vulnerability reporting: the **Security** tab of this repository, then **Report a
vulnerability**. Please include:

- pysanoid's version (`pysanoid.py --version`), the operating system and Python's version;
- the `sanoid.conf` involved, with pool and dataset names changed if you like;
- what pysanoid did, what you expected, and, if you can, the output of the same run with `--readonly --verbose`.

pysanoid is maintained by one person in their own time. Reports are read within a week, and a fix for a bug that can
lose data comes before anything else.

## Versions

Only the newest release is supported. A fix is released as a new version, with an entry in `AUDIT.md`, and, where
pysanoid then differs from sanoid, in `DEVIATIONS.md`.

## Bugs in sanoid itself

If the same bug is in Perl sanoid, please report it to sanoid as well, at
<https://github.com/jimsalterjrs/sanoid/issues>. pysanoid is an independent port, and its maintainers are not
sanoid's.
