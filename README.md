# Climate AI

One brain for three ecobees. Climate AI monitors every thermostat and room sensor in the house,
compares runtime against what the weather predicts, learns how the floors push heat into each
other, and adjusts schedules to minimize total HVAC runtime while occupied rooms stay comfortable.
Claude Opus 5.5 (through the Claude Agent SDK) reviews the results, explains them, and proposes
experiments. A deterministic controller enforces every limit.

**Status:** planning. Nothing is built yet.

- [`docs/BLUEPRINT.md`](docs/BLUEPRINT.md): the plan (house model, control rules, learning
  loop, weather normalization, Claude integration, architecture, roadmap)
- [`docs/blueprint.html`](docs/blueprint.html): the same plan with a working mockup of the
  app (open it in a browser)
