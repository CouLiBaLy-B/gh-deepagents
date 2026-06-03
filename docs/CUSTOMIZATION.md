# Customisation

## Ajouter un tool

Édite `src/gh_deepagent/tools.py` :

```python
from langchain_core.tools import tool

@tool
def my_tool(arg: str) -> str:
    """Description vue par le LLM — soigne-la."""
    ...
    return "result"
```

Ajoute-le à la liste retournée par `make_tools()`. Il sera disponible pour le main
agent et tous les sub-agents qui l'incluent.

## Ajouter / modifier un sub-agent

Dans `src/gh_deepagent/agent.py`, ajoute un dict à la liste `subagents`:

```python
{
  "name": "security-auditor",
  "description": "Audits the diff for OWASP top-10 issues.",
  "system_prompt": "...",
  "tools": tools,
  "model": "anthropic:claude-opus-4",  # optionnel: modèle différent
}
```

## Ajouter un skill (instructions réutilisables)

Dépose un `.md` dans `.deepagents/skills/`. Les Deep Agents les chargent
automatiquement comme commandes slash dans le CLI, ou comme contexte injectable
via `memory=[...]` côté SDK. Tu peux aussi installer les skills officiels :

```bash
git clone https://github.com/langchain-ai/langchain-skills
cd langchain-skills && ./install.sh --deepagents --global
```

## Brancher un autre LLM

Change `DEEPAGENT_MODEL`:
- `anthropic:claude-sonnet-4-5`
- `openai:gpt-4.1`
- `ollama:qwen2.5-coder:14b`
- `ollama:deepseek-coder-v2:16b`
- `google_genai:gemini-2.5-pro`

Le code utilise `langchain.chat_models.init_chat_model` donc tout LangChain chat
model marche.

## Activer l'approbation humaine (HITL) en local

Dans `src/gh_deepagent/agent.py`, passe `interrupt_on=` à `create_deep_agent`:

```python
agent = create_deep_agent(
    ...,
    interrupt_on={
        "finalize_patch": {"allowed_decisions": ["approve", "reject"]},
        "run_tests": {"allowed_decisions": ["approve", "edit", "reject"]},
    },
)
```

Combine avec un `checkpointer` LangGraph pour pouvoir reprendre.

## Tracing LangSmith

```bash
export LANGSMITH_TRACING=true
export LANGSMITH_API_KEY=lsv2_pt_...
export LANGSMITH_PROJECT=gh-deepagent
```

Toutes les traces (tool calls, sub-agents, LLM tokens) apparaissent dans
LangSmith — indispensable pour debugger l'agent.
