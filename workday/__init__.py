"""Workday automation for the Bizom QA desk: day start / end, VPN agent, APK auto-install,
ticket + PR watcher, daily report, run recorder, git helpers and the weekly Drive backup.

Stdlib only, except `workday.watch`, which runs under Greybox's virtualenv to reuse its
GET-only Zoho client. Nothing here edits Greybox's files or writes to Zoho / Bugzilla.
"""
