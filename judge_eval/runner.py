from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv
from loguru import logger

from .approaches import run_approach
from . import settings
from .llm_client import _estimate_cost, build_client
from .schemas import DatasetExample, JudgeModelConfig, JudgeRunRecord, RunConfig


def load_run_config(path: Path) -> RunConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return RunConfig.model_validate(data)


def load_dataset(path: Path) -> list[DatasetExample]:
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    out: list[DatasetExample] = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        row = json.loads(line)
        out.append(DatasetExample.model_validate(row))
    return out


def run_matrix(
    examples: list[DatasetExample],
    run_cfg: RunConfig,
    out_dir: Path,
    dry_run: bool = False,
    extra_env_file: Path | None = None,
) -> Path:
    load_dotenv()
    if extra_env_file is not None and extra_env_file.is_file():
        load_dotenv(extra_env_file, override=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "judges_raw.jsonl"
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "n_examples": len(examples),
        "models": [m.model_dump(by_alias=True) for m in run_cfg.models],
        "approaches": run_cfg.approaches,
    }
    (out_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    total = len(examples) * len(run_cfg.models) * len(run_cfg.approaches)
    logger.info(f"Starting eval: {len(examples)} examples × {len(run_cfg.models)} models × {len(run_cfg.approaches)} approaches = {total} calls")

    done = 0
    with log_path.open("w", encoding="utf-8") as fp:
        for ex in examples:
            for mc in run_cfg.models:
                client = build_client(mc) if not dry_run else None
                api_model = mc.api_model or os.getenv("XAI_MODEL", settings.DEFAULT_XAI_MODEL)
                for approach in run_cfg.approaches:
                    rec = _one_eval(
                        ex,
                        mc,
                        api_model,
                        approach,
                        run_cfg.hybrid,
                        client,
                        dry_run,
                    )
                    fp.write(rec.model_dump_json() + "\n")
                    fp.flush()
                    done += 1
                    if rec.error:
                        logger.warning(
                            f"[{done}/{total}] FAIL  {ex.id} | {mc.id} | {approach} — {rec.error}"
                        )
                    else:
                        verdict = "UNSAFE" if rec.pred_unsafe else "safe"
                        logger.info(
                            f"[{done}/{total}] OK    {ex.id} | {mc.id} | {approach} | "
                            f"{verdict} conf={rec.confidence:.2f} lat={rec.latency_ms:.0f}ms"
                        )
    logger.success(f"Done. Wrote {log_path}")
    return log_path


def _one_eval(
    ex: DatasetExample,
    mc: JudgeModelConfig,
    api_model: str,
    approach: str,
    hybrid: dict[str, float],
    client,
    dry_run: bool,
) -> JudgeRunRecord:
    if dry_run:
        return JudgeRunRecord(
            example_id=ex.id,
            task_type=ex.task_type,
            approach=approach,
            judge_model_id=mc.id,
            api_model=api_model,
            pred_unsafe=False,
            confidence=0.5,
            explanation="dry_run",
            latency_ms=0.0,
            human_unsafe=ex.human_unsafe,
            correct=None,
            hybrid_path="dry_run",
            dry_run=True,
        )

    try:
        from openai import OpenAI

        assert isinstance(client, OpenAI)
        v, r, meta = run_approach(client, api_model, approach, ex, hybrid)
        if meta.get("tokens"):
            pt, ct, tt = meta["tokens"]
        else:
            pt, ct, tt = r.prompt_tokens, r.completion_tokens, r.total_tokens
        lat = float(meta["latency_ms_total"]) if meta.get("latency_ms_total") is not None else float(r.latency_ms)
        cost = _estimate_cost(api_model, pt, ct) if pt is not None else None
        correct = None
        if ex.human_unsafe is not None:
            correct = v.pred_unsafe == ex.human_unsafe
        return JudgeRunRecord(
            example_id=ex.id,
            task_type=ex.task_type,
            approach=approach,
            judge_model_id=mc.id,
            api_model=api_model,
            pred_unsafe=v.pred_unsafe,
            confidence=v.confidence,
            explanation=v.explanation,
            latency_ms=lat,
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=tt,
            estimated_cost_usd=cost,
            human_unsafe=ex.human_unsafe,
            correct=correct,
            hybrid_path=meta.get("hybrid_path"),
        )
    except Exception as e:
        return JudgeRunRecord(
            example_id=ex.id,
            task_type=ex.task_type,
            approach=approach,
            judge_model_id=mc.id,
            api_model=api_model,
            pred_unsafe=False,
            confidence=0.0,
            explanation="",
            latency_ms=0.0,
            human_unsafe=ex.human_unsafe,
            correct=None,
            hybrid_path=None,
            error=str(e),
        )
