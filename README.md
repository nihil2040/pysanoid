# pysanoid

pysanoid takes and prunes ZFS snapshots by policy. It is a Python port of
[sanoid](https://github.com/jimsalterjrs/sanoid), the snapshot tool Jim Salter wrote and maintains with its
contributors. It reads sanoid's own configuration files, names its snapshots as sanoid does, and decides what to take
and what to destroy as sanoid does.

> **Published as-is.** pysanoid is a personal project, shared in case it is useful. It comes with no support and no
> promise of fixes or replies, and is used at your own risk. Forks are welcome: the GPL lets you change it and publish
> your own version. See [CONTRIBUTING.md](CONTRIBUTING.md).

- **sanoid's files and names, unchanged:** an existing `sanoid.conf` loads as it is, and a pool can move between
  pysanoid and Perl sanoid at any time.
- **Never destroy what the configuration keeps:** pysanoid follows sanoid one for one, except where following it
  could destroy a snapshot the configuration means to keep. Every such place is in [DEVIATIONS.md](DEVIATIONS.md).
- **One file, any system with ZFS:** Python 3.11 or later and its standard library, nothing else.

The port has been audited six times, five of them by running sanoid's own Perl beside pysanoid on the same input.
See [The Audit](HANDBOOK.adoc#audit) and [AUDIT.md](AUDIT.md). pysanoid takes and prunes snapshots only; it has no
syncoid.

## The handbook

Everything else is in **[HANDBOOK.adoc](HANDBOOK.adoc)**:

- [Why a Port](HANDBOOK.adoc#why-a-port): why pysanoid exists, and why not the system's own snapshot tasks
- [Quick Start Guide](HANDBOOK.adoc#quick-start): installing, a first read-only run, and the systemd units
- [Configuration](HANDBOOK.adoc#configuration): sections, templates, retention counts, preferred times, recursion,
  `prune_defer` and script hooks
- [pysanoid on the Author's Own System](HANDBOOK.adoc#own-system)
- [Differences from sanoid](HANDBOOK.adoc#differences), [The Audit](HANDBOOK.adoc#audit),
  [Testing](HANDBOOK.adoc#testing) and [Terminology](HANDBOOK.adoc#terminology)

## Quick start

```sh
sudo install -m 755 pysanoid.py /usr/local/sbin/pysanoid.py
sudo mkdir -p /etc/pysanoid
sudo install -m 644 sanoid.defaults.conf sanoid.conf /etc/pysanoid/   # then edit sanoid.conf for your pools
sudo /usr/local/sbin/pysanoid.py --cron --readonly --verbose        # shows what it would do, changes nothing
```

## Licence

pysanoid is licensed under the GNU General Public License v3.0, as sanoid is; see [LICENSE](LICENSE). sanoid is the
work of Jim Salter and its contributors, and pysanoid's behaviour is theirs, ported to Python and modified where
[DEVIATIONS.md](DEVIATIONS.md) says. pysanoid is an independent port, not affiliated with or endorsed by the sanoid
project.
