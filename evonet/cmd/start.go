package cmd

import (
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/evonic/evonet/internal/config"
	"github.com/evonic/evonet/internal/executor"
	"github.com/evonic/evonet/internal/ws"
)

// RunStart runs "evonet start" — connects once, exits when disconnected.
func RunStart(args []string) error {
	fs := flag.NewFlagSet("start", flag.ExitOnError)
	configPath := fs.String("config", "", "Path to config.yaml (optional)")
	server := fs.String("server", "", "Override server URL")
	token := fs.String("token", "", "Override connector token")
	workDir := fs.String("workdir", "", "Override working directory")
	verbose := fs.Bool("verbose", true, "Log incoming commands and their results")
	fs.Parse(args)

	cfg, err := config.Load(*configPath)
	if err != nil {
		return fmt.Errorf("failed to load config: %w", err)
	}
	config.ApplyCLI(cfg, config.CLIFlags{
		ServerURL: *server,
		Token:     *token,
		WorkDir:   *workDir,
	})
	if err := validateConfig(cfg); err != nil {
		return err
	}

	workdir := effectiveWorkDir(cfg)
	exec := executor.New(workdir, *verbose)
	client := ws.New(cfg, exec)
	handleSignals(client)

	log.SetFlags(log.Ltime)
	log.Printf("[evonet] Connecting to %s...", cfg.ServerURL)
	return client.RunOnce()
}

// RunRun runs "evonet run" — connects and auto-reconnects on failure.
func RunRun(args []string) error {
	fs := flag.NewFlagSet("run", flag.ExitOnError)
	configPath := fs.String("config", "", "Path to config.yaml (optional)")
	server := fs.String("server", "", "Override server URL")
	token := fs.String("token", "", "Override connector token")
	workDir := fs.String("workdir", "", "Override working directory")
	verbose := fs.Bool("verbose", true, "Log incoming commands and their results")
	fs.Parse(args)

	cfg, err := config.Load(*configPath)
	if err != nil {
		return fmt.Errorf("failed to load config: %w", err)
	}
	config.ApplyCLI(cfg, config.CLIFlags{
		ServerURL: *server,
		Token:     *token,
		WorkDir:   *workDir,
	})
	if err := validateConfig(cfg); err != nil {
		return err
	}

	workdir := effectiveWorkDir(cfg)
	exec := executor.New(workdir, *verbose)
	client := ws.New(cfg, exec)
	handleSignals(client)

	log.SetFlags(log.Ltime)
	log.Printf("[evonet] Starting (auto-reconnect)...")
	client.Run()
	return nil
}

// handleSignals stops the client gracefully on the first SIGINT/SIGTERM and
// force-exits on the second, so a hung shutdown can't swallow CTRL+C forever.
func handleSignals(client *ws.Client) {
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		sig := <-sigCh
		log.Printf("[evonet] Received %v, shutting down...", sig)
		go client.Stop()
		sig = <-sigCh
		log.Printf("[evonet] Received %v again, forcing exit", sig)
		os.Exit(130)
	}()
}

// RunStatus prints the current config status.
func RunStatus(args []string) error {
	fs := flag.NewFlagSet("status", flag.ExitOnError)
	configPath := fs.String("config", "", "Path to config.yaml (optional)")
	fs.Parse(args)

	cfg, err := config.Load(*configPath)
	if err != nil {
		fmt.Println("No config found. Run 'evonet pair' first.")
		return nil
	}
	if cfg.ConnectorToken == "" {
		fmt.Println("Not paired. Run 'evonet pair' first.")
		return nil
	}
	fmt.Printf("Server:  %s\n", cfg.ServerURL)
	fmt.Printf("Home:    %s (%s)\n", cfg.HomeName, cfg.HomeID)
	fmt.Printf("Token:   %s...\n", cfg.ConnectorToken[:min(8, len(cfg.ConnectorToken))])
	fmt.Printf("WorkDir: %s\n", effectiveWorkDir(cfg))
	return nil
}

// RunUnpair clears the pairing config.
func RunUnpair(args []string) error {
	cfg := &config.Config{}
	if err := config.Save(cfg); err != nil {
		return fmt.Errorf("failed to clear config: %w", err)
	}
	fmt.Println("Unpaired successfully.")
	return nil
}

func validateConfig(cfg *config.Config) error {
	if cfg.ConnectorToken == "" {
		return fmt.Errorf("not paired — run 'evonet pair' first")
	}
	if cfg.ServerURL == "" {
		return fmt.Errorf("server URL not configured — run 'evonet pair' first")
	}
	// Re-validate in case --server override bypasses the pair-time check.
	if err := validateServerURL(cfg.ServerURL); err != nil {
		return fmt.Errorf("invalid server URL: %w", err)
	}
	return nil
}

func effectiveWorkDir(cfg *config.Config) string {
	if cfg.WorkDir != "" {
		return cfg.WorkDir
	}
	// Default to the directory of the binary
	exe, err := os.Executable()
	if err != nil {
		cwd, _ := os.Getwd()
		return cwd
	}
	return exe[:len(exe)-len("/evonet")]
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
