//go:build windows

package executor

import "os/exec"

// detachTTY is a no-op on Windows: there is no POSIX controlling terminal
// or foreground process group to steal.
func detachTTY(cmd *exec.Cmd) {}
