# Security policy

## Reporting a vulnerability

Please report it **privately**, not in a public issue or pull request:

1. Go to the repository's **Security** tab.
2. Choose **Report a vulnerability** (GitHub's private vulnerability reporting).

I'll answer as soon as I can. This is a small project run by one person, so I can't
promise a response time, but a private report will be read and taken seriously.

## What counts

This integration holds a device's local key and talks to a device on your home
network, so these matter most:

- the local key, device ID, host, or other private data being logged, stored, exposed
  in diagnostics, or sent anywhere
- anything that makes the integration set a data point or send a command to the
  vacuum (it is meant to be read-only)
- anything that exposes the map, room names or the layout of a home to someone who
  shouldn't see them
- a crafted message from the device that crashes Home Assistant or runs code

Bugs that aren't security problems (a wrong map, a missing room) go in a normal issue.

## What to leave out

Please don't put real values in a report: redact the local key, device ID, IP address,
Tuya user ID and `mapId`, and don't attach a real map. Made-up values that show the
problem are enough.

## Supported versions

Only the latest release is supported. There is no release yet, so for now that means
`main`.
