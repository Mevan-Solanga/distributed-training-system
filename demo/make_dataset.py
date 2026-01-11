from pathlib import Path

# Creates simple shard files with line-based "samples"
# data/shards/shard_00000.txt, shard_00001.txt, ...

def main(num_shards: int = 8, lines_per_shard: int = 50):
    out_dir = Path("data/shards")
    out_dir.mkdir(parents=True, exist_ok=True)

    global_id = 0
    for s in range(num_shards):
        p = out_dir / f"shard_{s:05d}.txt"
        with p.open("w", encoding="utf-8") as f:
            for _ in range(lines_per_shard):
                f.write(f"sample_id={global_id}, shard={s}\n")
                global_id += 1

    print(f"Created {num_shards} shards in {out_dir} ({lines_per_shard} lines each).")


if __name__ == "__main__":
    main()
