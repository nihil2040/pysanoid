package FakeTime;
# `perl -MFakeTime sanoid ...`: upstream's whole script with time() frozen at $ENV{FAKE_NOW}. Loaded before the
# script is compiled, so every time() in it is this one; upstream reads the clock only through time().
sub import {
    my $now = $ENV{FAKE_NOW};
    die "FakeTime: FAKE_NOW is not set\n" unless defined $now;
    no warnings 'once';
    *CORE::GLOBAL::time = sub () { $now + 0 };
}
1;
