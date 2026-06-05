import asyncio
import json
import os
from google import genai
from google.genai import types

_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MONGODB_MCP_CONFIG = {
    "mcpServers": {
        "mongodb": {
            "command": "npx",
            "args": [
                "-y",
                "@mongodb-js/mongodb-mcp-server",
                "--connectionString",
                os.environ["MONGODB_URI"]
            ]
        }
    }
}

AGENT_SYSTEM_PROMPT = """You are CrisisWatch, an AI disease outbreak surveillance agent.

You have access to MongoDB tools to query and store outbreak data.

Your reasoning pipeline for any new cluster signal:
1. Use find_documents to query the 'outbreaks' collection for historical cases of this disease in this region
2. Use find_documents to query 'cluster_signals' for seasonal patterns matching this disease and trigger
3. Reason: if historical matches exist with same seasonal trigger → REGULAR. If anomaly score > 0.7 with no pattern match → NOVEL
4. Calculate severity relative to population (cases/population * 100000 = case rate)
5. Predict spread regions based on transmission mode, climate corridors, population movement
6. Generate audience-specific safety advice (public, practitioner, NGO)
7. Use insert_document to save the final alert to the 'outbreaks' collection

Always return a final JSON summary of your reasoning and conclusions.
Be specific, factual, and epidemiologically sound.
"""

async def run_mcp_agent(signal_data: dict) -> dict:
    """Run Gemini as a true agentic loop with MongoDB MCP tools"""
    
    prompt = f"""
Analyze this new disease cluster signal and process it through your full reasoning pipeline:

{json.dumps(signal_data, indent=2)}

Database: crisiswatch
Available collections: outbreaks, cluster_signals, seasonal_patterns, health_workers

Execute each step of your pipeline using the MongoDB tools available to you.
After completing all steps, return a JSON object with:
{{
  "classification": "novel|regular|pending",
  "severity": "critical|high|moderate|low",
  "anomaly_score": 0.0,
  "ai_assessment": "detailed reasoning paragraph",
  "ai_summary": "3-sentence situational brief for health authorities",
  "spread_prediction": {{
    "target_regions": [],
    "confidence": 0.0,
    "reasoning": "",
    "timeframe": ""
  }},
  "safety_advice": {{
    "public": "",
    "practitioner": "",
    "ngo": ""
  }}
}}
"""

    try:
        response = _client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=AGENT_SYSTEM_PROMPT,
                temperature=0.2,
                max_output_tokens=2048,
                tools=[
                    types.Tool(
                        function_declarations=get_mongodb_tool_declarations()
                    )
                ]
            )
        )
        
        # Extract JSON from response
        text = response.text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
            
        return json.loads(text)
        
    except Exception as e:
        print(f"[MCP Agent error] {e}")
        return None

def get_mongodb_tool_declarations():
    """Declare MongoDB operations as Gemini function tools"""
    return [
        types.FunctionDeclaration(
            name="find_documents",
            description="Query documents from a MongoDB collection",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "collection": types.Schema(
                        type=types.Type.STRING,
                        description="Collection name to query"
                    ),
                    "filter": types.Schema(
                        type=types.Type.STRING,
                        description="JSON filter query"
                    ),
                    "limit": types.Schema(
                        type=types.Type.INTEGER,
                        description="Max documents to return"
                    )
                },
                required=["collection"]
            )
        ),
        types.FunctionDeclaration(
            name="insert_document",
            description="Insert a document into a MongoDB collection",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "collection": types.Schema(
                        type=types.Type.STRING,
                        description="Collection name"
                    ),
                    "document": types.Schema(
                        type=types.Type.STRING,
                        description="JSON document to insert"
                    )
                },
                required=["collection", "document"]
            )
        )
    ]

def execute_tool_call(tool_name: str, tool_args: dict, db) -> str:
    """Execute MongoDB tool calls from Gemini"""
    try:
        if tool_name == "find_documents":
            collection = db[tool_args["collection"]]
            filter_query = json.loads(tool_args.get("filter", "{}"))
            limit = tool_args.get("limit", 10)
            docs = list(collection.find(filter_query).limit(limit))
            # Convert ObjectId to string
            for doc in docs:
                if "_id" in doc:
                    doc["_id"] = str(doc["_id"])
            return json.dumps(docs)
            
        elif tool_name == "insert_document":
            collection = db[tool_args["collection"]]
            document = json.loads(tool_args["document"])
            result = collection.insert_one(document)
            return json.dumps({"inserted_id": str(result.inserted_id)})
            
    except Exception as e:
        return json.dumps({"error": str(e)})

async def run_mcp_agent_with_tools(signal_data: dict, db) -> dict:
    """
    Full agentic loop: Gemini reasons, calls MongoDB tools, 
    gets results, continues reasoning until done
    """
    from google.genai import types as gtypes
    
    messages = [
        {
            "role": "user",
            "parts": [{"text": f"""
Analyze this disease cluster and run your full surveillance pipeline.
Use MongoDB tools to check history, then classify, assess severity,
predict spread, and generate advice.

Signal data:
{json.dumps(signal_data, indent=2)}

Database: crisiswatch

After all tool calls, return final JSON with:
classification, severity, anomaly_score, ai_assessment, ai_summary,
spread_prediction (target_regions, confidence, reasoning, timeframe),
safety_advice (public, practitioner, ngo)
"""}]
        }
    ]
    
    tools = [gtypes.Tool(function_declarations=get_mongodb_tool_declarations())]
    
    max_iterations = 6
    iteration = 0
    
    while iteration < max_iterations:
        iteration += 1
        
        response = _client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=messages,
            config=gtypes.GenerateContentConfig(
                system_instruction=AGENT_SYSTEM_PROMPT,
                temperature=0.2,
                max_output_tokens=2048,
                tools=tools
            )
        )
        
        candidate = response.candidates[0]
        
        # Check if Gemini wants to call a tool
        tool_calls = [
            part for part in candidate.content.parts 
            if hasattr(part, 'function_call') and part.function_call
        ]
        
        if tool_calls:
            # Execute all tool calls and feed results back
            tool_results = []
            for part in tool_calls:
                fc = part.function_call
                result = execute_tool_call(fc.name, dict(fc.args), db)
                tool_results.append({
                    "role": "tool",
                    "parts": [{
                        "function_response": {
                            "name": fc.name,
                            "response": {"result": result}
                        }
                    }]
                })
                print(f"[MCP Tool] {fc.name} called → {result[:100]}...")
            
            # Add assistant message and tool results to history
            messages.append({"role": "model", "parts": [p.__dict__ for p in candidate.content.parts]})
            messages.extend(tool_results)
            
        else:
            # No more tool calls — extract final JSON
            text = response.text.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            
            try:
                return json.loads(text)
            except:
                print(f"[MCP Agent] Could not parse final JSON: {text[:200]}")
                return None
    
    print("[MCP Agent] Max iterations reached")
    return None