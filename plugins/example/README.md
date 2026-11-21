# example plugin

A minimal plugin that shows how an organization-specific add-on plugs into sf. The original plugin of this repository held one organization's internal tools and is not part of this archive.

- cron: one script job and three prompt jobs (`cron.toml`, `prompts/`, `scripts/`)
- skill: `skills/example-dev-env-ops/`
- settings: `plugin.toml` (`[config]`, `[template_names]`, `[defaults]`)

Turn it on with `plugins = ["example"]` in `envs/<env>.toml`, and off by removing the name.
