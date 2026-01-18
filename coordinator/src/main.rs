use anyhow::Result;
use clap::Parser;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::process::Command;
use tokio::time::sleep;
use tracing::{error, info, warn};

#[derive(Parser)]
#[command(name = "Distributed Training Coordinator")]
#[command(about = "Manages worker processes, heartbeats, and recovery")]
struct Args {
    /// Job ID
    #[arg(long, default_value = "demo-job")]
    job_id: String,

    /// Number of workers
    #[arg(long, default_value = "4")]
    world_size: usize,

    /// Checkpoint directory
    #[arg(long, default_value = "./checkpoints")]
    checkpoint_dir: String,

    /// Checkpoint every N steps
    #[arg(long, default_value = "5")]
    checkpoint_every: usize,

    /// Sleep between steps (seconds)
    #[arg(long, default_value = "0.5")]
    sleep_sec: f64,

    /// Dataset directory
    #[arg(long, default_value = "./data/shards")]
    dataset_dir: String,

    /// Heartbeat timeout (seconds)
    #[arg(long, default_value = "10")]
    heartbeat_timeout: u64,

    /// Max restarts per worker
    #[arg(long, default_value = "50")]
    max_restarts: usize,

    /// Enable S3
    #[arg(long, default_value = "false")]
    use_s3: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct WorkerHeartbeat {
    timestamp: f64,
    rank: usize,
    pid: u32,
}

#[derive(Debug)]
struct Worker {
    rank: usize,
    child: tokio::process::Child,
    restarts: usize,
    last_heartbeat: f64,
}

struct Coordinator {
    job_id: String,
    world_size: usize,
    checkpoint_dir: PathBuf,
    dataset_dir: PathBuf,
    workers: HashMap<usize, Worker>,
    max_restarts: usize,
    heartbeat_timeout: Duration,
    args: Args,
}

impl Coordinator {
    fn new(args: Args) -> Self {
        Self {
            job_id: args.job_id.clone(),
            world_size: args.world_size,
            checkpoint_dir: PathBuf::from(&args.checkpoint_dir),
            dataset_dir: PathBuf::from(&args.dataset_dir),
            workers: HashMap::new(),
            max_restarts: args.max_restarts,
            heartbeat_timeout: Duration::from_secs(args.heartbeat_timeout),
            args,
        }
    }

    async fn spawn_worker(&self, rank: usize) -> Result<tokio::process::Child> {
        let env_vars = vec![
            ("JOB_ID", self.job_id.clone()),
            ("RANK", rank.to_string()),
            ("WORLD_SIZE", self.world_size.to_string()),
            ("CHECKPOINT_DIR", self.checkpoint_dir.to_string_lossy().to_string()),
            ("CHECKPOINT_EVERY", self.args.checkpoint_every.to_string()),
            ("SLEEP_SEC", self.args.sleep_sec.to_string()),
            ("DATASET_DIR", self.dataset_dir.to_string_lossy().to_string()),
            ("USE_S3", if self.args.use_s3 { "1" } else { "0" }.to_string()),
        ];

        let mut cmd = Command::new("python");
        cmd.arg("demo/worker.py");
        cmd.current_dir("."); // Project root

        for (k, v) in env_vars {
            cmd.env(k, v);
        }

        cmd.stdout(Stdio::inherit()).stderr(Stdio::inherit());

        let child = cmd.spawn()?;
        info!("[coord] spawned worker rank={} pid={}", rank, child.id().unwrap_or(0));

        Ok(child)
    }

    async fn check_heartbeat(&self, rank: usize) -> bool {
        let heartbeat_file = self
            .checkpoint_dir
            .join(&self.job_id)
            .join(format!("worker_{}", rank))
            .join("HEARTBEAT");

        if !heartbeat_file.exists() {
            return false;
        }

        match std::fs::read_to_string(&heartbeat_file) {
            Ok(content) => {
                if let Ok(hb) = serde_json::from_str::<WorkerHeartbeat>(&content) {
                    let now = SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_secs_f64();
                    (now - hb.timestamp) < self.heartbeat_timeout.as_secs_f64()
                } else {
                    false
                }
            }
            Err(_) => false,
        }
    }

    async fn monitor_workers(&mut self) {
        loop {
            sleep(Duration::from_millis(500)).await;

            let mut to_restart = Vec::new();

            for (rank, worker) in self.workers.iter_mut() {
                match worker.child.try_wait() {
                    Ok(Some(status)) => {
                        if status.success() {
                            info!("[coord] worker rank={} completed (exit 0)", rank);
                            to_restart.push(*rank);
                        } else {
                            warn!("[coord] worker rank={} exited with status {}", rank, status);
                            to_restart.push(*rank);
                        }
                    }
                    Ok(None) => {
                        // Still running; check heartbeat
                        if !self.check_heartbeat(*rank).await {
                            warn!("[coord] worker rank={} heartbeat timeout!", rank);
                            let _ = worker.child.kill().await;
                            to_restart.push(*rank);
                        }
                    }
                    Err(e) => {
                        error!("[coord] failed to check worker {}: {}", rank, e);
                        to_restart.push(*rank);
                    }
                }
            }

            for rank in to_restart {
                if let Some(mut w) = self.workers.remove(&rank) {
                    if w.restarts >= self.max_restarts {
                        warn!("[coord] rank={} max restarts hit; not restarting", rank);
                        continue;
                    }

                    w.restarts += 1;
                    info!("[coord] restarting rank={} (attempt {}/{})", rank, w.restarts, self.max_restarts);

                    sleep(Duration::from_millis(500)).await;

                    match self.spawn_worker(rank).await {
                        Ok(child) => {
                            let now = SystemTime::now()
                                .duration_since(UNIX_EPOCH)
                                .unwrap_or_default()
                                .as_secs_f64();
                            self.workers.insert(
                                rank,
                                Worker {
                                    rank,
                                    child,
                                    restarts: w.restarts,
                                    last_heartbeat: now,
                                },
                            );
                        }
                        Err(e) => {
                            error!("[coord] failed to restart rank={}: {}", rank, e);
                        }
                    }
                }
            }

            if self.workers.is_empty() {
                info!("[coord] all workers done. job completed.");
                break;
            }
        }
    }

    async fn run(&mut self) -> Result<()> {
        info!("[coord] job={}", self.job_id);
        info!("[coord] world_size={}", self.world_size);
        info!("[coord] checkpoints={}", self.checkpoint_dir.display());

        // Spawn all workers
        for rank in 0..self.world_size {
            let child = self.spawn_worker(rank).await?;
            let now = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64();
            self.workers.insert(
                rank,
                Worker {
                    rank,
                    child,
                    restarts: 0,
                    last_heartbeat: now,
                },
            );
        }

        info!("[coord] all workers spawned");

        // Monitor workers
        self.monitor_workers().await;

        info!("[coord] coordinator shutdown");
        Ok(())
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt::init();

    let args = Args::parse();
    let mut coordinator = Coordinator::new(args);

    coordinator.run().await?;

    Ok(())
}
