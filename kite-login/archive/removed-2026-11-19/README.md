# Removed 19 Nov 2026: Claude Skill Desk and Agent Pipeline

Removed from the app at the user's request ("they are of no use now"). This folder keeps their desk-only code so nothing is lost.
It is outside the app: nothing here is imported or built.

- `backend/strategies/skill_desk.py`, `backend/routes/skill_desk.py`: the Claude Skill Desk.
- `frontend/pages/ClaudeSkillDesk.jsx`, `frontend/pages/AgentPipeline.jsx`: their pages.
- `tests/test_skill_desk.py`: its tests.

Still in the app as shared code: `strategies/agent_desk.py` (the Multi Agent Team uses its helpers; the AgentDesk class is no longer run)
and `strategies/pipeline_swing.py` (Expiry Range and the IPO Agent use its data helpers; the swing desk is no longer run).
Past trade records stay in `backend/.orb_cache/` (`skill_desk/`, `pipeline/`, `pipeline_swing/`, `desk_pipeline.json`).
