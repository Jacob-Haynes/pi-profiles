# SSD tips for the Pi 5

`bootstrap.sh` warns when `/` is not on an SSD. It never changes the EEPROM. `--tune-ssd` applies only the journald cap and the swap size.

## Boot from the SSD

1. Flash Raspberry Pi OS Bookworm 64-bit onto the SSD with Raspberry Pi Imager. Set the hostname, user, SSH and Wi-Fi country in Imager's settings.
2. Update the bootloader from a running system: `sudo rpi-eeprom-update -a && sudo reboot`.
3. Set the boot order: `sudo rpi-eeprom-config --edit`. The digits are read right to left:
   - NVMe HAT: `BOOT_ORDER=0xf416` (NVMe, then SD, then USB)
   - USB SSD: `BOOT_ORDER=0xf14` (USB, then SD)
   - For an NVMe HAT on a non-HAT+ board, also add `PCIE_PROBE=1`
4. Check: `findmnt /` should show `/dev/nvme0n1p2` or `/dev/sda2`.

Keep a spare SD card with the same image. If the SSD fails at a demo, the Pi falls back to SD.

## Swap

Bookworm uses `dphys-swapfile` with a 100 MB swap by default. On an SSD a 2 GB swap is harmless and saves you from out-of-memory kills when OTS, a YOLO model and the detectors all load at once:

```bash
sudo sed -i 's/^#\?CONF_SWAPSIZE=.*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile
sudo systemctl restart dphys-swapfile
```

(`sudo ./bootstrap.sh --tune-ssd` does the same.)

## Logs: persistent journald, capped

Raspberry Pi OS sets `Storage=volatile` in `/etc/systemd/journald.conf`, so logs live in RAM and **vanish on a freeze or power cut**, exactly when you need them. bootstrap.sh therefore always writes:

```ini
# /etc/systemd/journald.conf.d/isr-kit.conf   (written by bootstrap.sh on every run)
[Journal]
Storage=persistent
SystemMaxUse=200M
RuntimeMaxUse=50M
MaxRetentionSec=7day
```

The cap keeps SSD wear and disk use trivial. After a crash, `journalctl -b -1` shows the previous boot. `log2ram` isn't recommended here: it loses the most recent logs on a power cut.

## Other

- `noatime` on `/` in `/etc/fstab` cuts writes. Imager images already use `defaults,noatime`, so check before editing.
- Use the official 27 W USB-C PSU. An SSD plus SDR plus Alfa adapter plus capture card can trip under-voltage on weaker supplies, and the launcher's throttling readout will show it.
- Put a powered USB 3 hub between the Pi and the Alfa adapter and capture card if you see `under-voltage has occurred`.
- Use active cooling (the official Active Cooler). `demo-full` and the YOLO profiles hold all four cores high.
