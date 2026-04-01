# pi-profiles

Simple systemd profile switcher for Raspberry Pi. Run multiple projects on one Pi, switch between them with a single command.

## Install

```bash
sudo bash install.sh
```

## Usage

```bash
sudo pi-profiles bft          # switch to BFT profile
sudo pi-profiles --list        # list available profiles
sudo pi-profiles --status      # show active profile and service states
```

The active profile starts automatically on boot.

## Adding a profile

Create a file in `/opt/pi-profiles/profiles.d/<name>.conf` listing one systemd service per line:

```
# My Project
my-service-a
my-service-b
nginx
```

Then switch to it: `sudo pi-profiles my-project`

## How it works

- Each profile is a `.conf` file listing systemd services
- `switch.sh` stops the old profile's services and starts the new ones
- The active profile name is stored in `/opt/pi-profiles/active`
- A oneshot systemd service (`pi-profiles.service`) starts the active profile on boot