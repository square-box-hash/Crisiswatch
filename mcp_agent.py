import json
import os
from google import genai
from google.genai import types

_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


AGENT_SYSTEM_PROMPT = """
You are CrisisWatch Verification Agent.

Your ONLY role is to:
1. Verify outbreak reasoning produced by upstream pipeline
2. Explain why a classification is correct or questionable
3. Cross-check with MongoDB tool results
4. Enhance interpretability of spread trajectory and safety advice

You MUST NOT:
- create new outbreak classifications
- override severity or confidence
- generate new predictions
- replace pipeline outputs

You are an audit and reasoning layer only.
"""


# ─────────────────────────────────────────────
# TOOL DEFINITION (MongoDB unchanged)
# ─────────────────────────────────────────────

def get_mongodb_tool_declarations():
    return [
        types.FunctionDeclaration(
            name="find_documents",
            description="Query MongoDB collection",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "collection": types.Schema(type=types.Type.STRING),
                    "filter": types.Schema(type=types.Type.STRING),
                    "limit": types.Schema(type=types.Type.INTEGER),
                },
                required=["collection"]
            )
        )
    ]


# ─────────────────────────────────────────────
# CORE MCP VERIFICATION FUNCTION
# ─────────────────────────────────────────────

async def run_mcp_verification(pipeline_output: dict, db) -> dict:
    """
    MCP acts as:
    - verifier
    - explainer
    - reasoning auditor
    NOT a decision maker
    """

    prompt = f"""
You are analyzing an already-generated outbreak report.

PIPELINE OUTPUT:
{json.dumps(pipeline_output, indent=2)}

Your tasks:
1. Check if classification is consistent with historical MongoDB patterns
2. Check if severity aligns with cases and region history
3. Evaluate whether spread trajectory is reasonable (do NOT change it)
4. Improve clarity of safety advice if needed
5. Provide reasoning critique

Return ONLY JSON:

{{
  "verification_status": "confirmed | questionable | inconsistent",
  "reasoning_audit": "why this decision makes sense or not",
  "historical_alignment": "matches | partial | mismatch",
  "trajectory_feedback": "commentary on spread prediction validity",
  "safety_advice_review": {{
    "public": "",
    "practitioner": "",
    "ngo": ""
  }},
  "confidence_in_pipeline": 0.0
}}
"""

    try:
        response = _client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=AGENT_SYSTEM_PROMPT,
                temperature=0.2,
                max_output_tokens=1500,
                tools=[
                    types.Tool(
                        function_declarations=get_mongodb_tool_declarations()
                    )
                ]
            )
        )

        text = response.text.strip()

        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        return json.loads(text)

    except Exception as e:
        print(f"[MCP Verification Error] {e}")
        return {
            "verification_status": "unknown",
            "reasoning_audit": "MCP failed to execute",
            "historical_alignment": "unknown",
            "trajectory_feedback": "",
            "safety_advice_review": {},
            "confidence_in_pipeline": 0.5
        }