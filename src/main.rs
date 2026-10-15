//! evaTorrent Command Line Interface (`evatorrent web` / `evatorrent download`).

#![allow(clippy::too_many_arguments)]
#![allow(clippy::type_complexity)]
#![allow(clippy::should_implement_trait)]

use std::io::Write;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use clap::{Parser, Subcommand};
use tracing::{error, info};

use evatorrent::engine::session::TorrentSession;
use evatorrent::torrent::Torrent;
use evatorrent::web::app::run_web_server;

#[derive(Parser, Debug)]
#[command(
    name = "evatorrent",
    version,
    about = "evaTorrent ⚡ - High-performance modern BitTorrent engine and Web UI (Rust/Tokio)"
)]
struct Cli {
    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Subcommand, Debug)]
enum Commands {
    /// Start the evaTorrent Web UI and REST/WebSocket server
    Web {
        /// Host address to bind to
        #[arg(long, default_value = "127.0.0.1", env = "EVA_HOST")]
        host: String,

        /// Port to listen on
        #[arg(long, default_value_t = 8080, env = "EVA_PORT")]
        port: u16,
    },
    /// Download a .torrent file directly in the CLI
    Download {
        /// Path to the .torrent file
        torrent: PathBuf,

        /// Output directory for downloaded files
        #[arg(short, long, env = "DOWNLOAD_DIR", default_value = "downloads")]
        output: PathBuf,

        /// Enable sequential download mode (stream order)
        #[arg(long, default_value_t = false)]
        sequential: bool,
    },
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let cli = Cli::parse();

    match cli.command {
        Some(Commands::Web { host, port }) => {
            run_web_server(&host, port).await?;
        }
        Some(Commands::Download {
            torrent,
            output,
            sequential,
        }) => {
            if !torrent.exists() {
                error!("File not found: {}", torrent.display());
                std::process::exit(1);
            }
            std::fs::create_dir_all(&output)?;

            info!("Loading torrent from {}...", torrent.display());
            let parsed = Torrent::from_file(&torrent)?;
            info!("Torrent Name: {}", parsed.name);
            info!(
                "Total Size: {:.2} MB",
                parsed.total_length as f64 / 1024.0 / 1024.0
            );
            info!(
                "Pieces: {} ({} bytes each)",
                parsed.piece_count, parsed.piece_length
            );
            info!("Trackers: {}", parsed.trackers.join(", "));

            let session = Arc::new(TorrentSession::new(
                parsed, output, 50, 6881, None, None, None, None, None,
            )?);
            if sequential {
                session.set_sequential(true);
            }
            session.start();

            info!("Session started. Downloading pieces from swarm...");
            while !session.piece_manager.is_complete() {
                tokio::time::sleep(Duration::from_secs(1)).await;
                let pct = session.piece_manager.progress_percentage();
                let dl_mb = session.download_speed() / 1024.0 / 1024.0;
                let (active, seen) = session.peer_counts();
                let eta = session.eta_seconds().unwrap_or(0);
                print!(
                    "\rProgress: {:6.2}% | Speed: {:5.2} MB/s | Peers: {}/{} | ETA: {}s  ",
                    pct, dl_mb, active, seen, eta
                );
                let _ = std::io::stdout().flush();
            }
            println!();
            info!("Download completed successfully!");
            session.stop(false).await;
        }
        None => {
            // Default to starting the web UI on 0.0.0.0:8080
            run_web_server("0.0.0.0", 8080).await?;
        }
    }

    Ok(())
}
