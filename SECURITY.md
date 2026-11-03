# Security policy

pysanoid is published as-is, with no promise of support, fixes or a reply. See the GPL's "Disclaimer of Warranty"
in [LICENSE](LICENSE). This page says what to do if you find a serious bug anyway.

pysanoid runs as root and destroys ZFS snapshots. A bug that makes it destroy a snapshot the configuration means to
keep, or stop taking snapshots without saying so, can cost someone their data. Such a bug is treated here as a
security problem, whether or not it involves an attacker. So is any way for someone other than root to make pysanoid
run a command, read a file it shouldn't, or act on a dataset it isn't configured for.

## Reporting

Use GitHub's private vulnerability reporting: the **Security** tab of this repository, then **Report a
vulnerability**. Please include pysanoid's version (`pysanoid.py --version`), the `sanoid.conf` involved, and what
pysanoid did, with the output of the same run with `--readonly --verbose` if you can.

A report may be read and fixed, or it may not. If you need a fix, you are welcome to make it in a fork; the licence
lets you.

## Bugs in sanoid itself

If the same bug is in Perl sanoid, please report it to sanoid as well, at
<https://github.com/jimsalterjrs/sanoid/issues>. pysanoid is an independent port, and its maintainer is not
sanoid's.
