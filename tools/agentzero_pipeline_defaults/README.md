# AgentZero Live Pipeline

This directory is seeded into `/app/work_dir/assets/agentzero_pipeline/` inside
the Hostinger/Coolify container.

After the infrastructure loader is deployed, editing changes should happen here,
not through Docker rebuilds:

- `pipeline_config.json` controls the editing preset, intro teaser, LLM,
  audio, logo placeholder paths, and YouTube defaults.
- `phone_render_worker.py` is the live render script. The upload app starts it
  from this mounted directory for every render.
- `audio/` stores replaceable chime, whoosh, and music-bed assets.
- `logo/` is reserved for later Remotion/logo-animation assets.

The Docker image keeps reference copies in `defaults/`. Existing live files are
not overwritten on container startup, so tuning survives restarts and redeploys.
