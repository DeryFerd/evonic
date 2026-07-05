//go:build !windows

package executor

import (
	"os/exec"
	"syscall"
)

// detachTTY runs cmd in a new session so it has no controlling terminal.
// Without this, the forced-interactive login shell used for env capture
// opens /dev/tty and makes itself the terminal's foreground process group
// (bash ≥4.4 and zsh both do this); when it exits, evonet is left in the
// background of its own TTY and never receives CTRL+C again.
func detachTTY(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
}
