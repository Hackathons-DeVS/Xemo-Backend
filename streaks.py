import requests
import json
import time
import os
import re # Import re for JSON extraction

from gemini_config import call_gemini_with_rate_limit_retry, create_gemini_client, get_gemini_api_key
from model_routing import get_model_for

# --- Configuration ---
# Set a reasonable timeout for the single, potentially longer API call
# Increase this if generating plans for very large documents still times out
API_TIMEOUT = 120  # seconds (Increased timeout for the single large call)

# --- Helper Functions ---

def get_mindmap_code(mindmap):
    return (
        mindmap.get('code')
        or mindmap.get('mindmap_code')
        or mindmap.get('content')
        or ''
    )

def test_api_connection():
    """Test if the API connection works"""
    if not get_gemini_api_key():
        print("WARNING: API key not configured. Please set GEMINI_API_KEY.")
        return False

    client = create_gemini_client(timeout=10)
    try:
        client.models.list() # More reliable test than chat completion
        print("API Connection Test Successful.")
        return True
    except Exception as e:
        print(f"API Connection Error: {e}")
        print("Please check your API key, base URL, and network connectivity.")
        return False

def create_fallback_quiz(topic, subtopic):
    """Create a fallback quiz when API fails or JSON is invalid"""
    print(f"Warning: Creating fallback quiz for {topic} -> {subtopic}")
    return [
        {
            "question": f"What is a key concept related to {subtopic} within the topic of {topic}?",
            "options": [
                "A. Option A",
                "B. Option B",
                "C. Option C",
                "D. Option D"
            ],
            "answer": "A" # Placeholder answer
        },
        {
            "question": f"How does {subtopic} relate to the broader topic of {topic}?",
            "options": [
                "A. Relation A",
                "B. Relation B",
                "C. Relation C",
                "D. Relation D"
            ],
            "answer": "B" # Placeholder answer
        }
    ]

def parse_json_from_response(response_content):
    """Extracts and parses JSON from the potentially messy AI response string."""
    try:
        # Try parsing directly first
        return json.loads(response_content)
    except json.JSONDecodeError:
        # If direct parsing fails, try extracting JSON block
        print("Warning: Direct JSON parsing failed, attempting extraction.")
        # Regex to find JSON object starting with { and ending with }
        match = re.search(r'\{.*\}', response_content, re.DOTALL)
        if match:
            json_str = match.group(0)
            try:
                return json.loads(json_str)
            except json.JSONDecodeError as e:
                print(f"Error: Failed to parse extracted JSON: {e}")
                print(f"Extracted String: {json_str[:500]}...") # Log the problematic string
                return None
        else:
            print("Error: No JSON object found in the response.")
            print(f"Response Content: {response_content[:500]}...") # Log the problematic response
            return None

# --- Core Function ---

def generate_study_plan_and_quizzes(mindmap_data=None, pdf_text=None):
    """
    Generate a study plan with topics, subtopics, durations, AND quizzes
    in a single API call for better performance.
    """
    if not mindmap_data:
        print("Error: No mindmap data provided to generate_study_plan_and_quizzes")
        raise ValueError("No mindmap data provided")

    if not test_api_connection():
         # If API test fails, immediately raise an error or return a failure indicator
         # This prevents attempting the main call which will also likely fail.
         raise ConnectionError("API connection failed. Cannot generate study plan.")


    # Create a client with explicit timeout
    client = create_gemini_client(timeout=API_TIMEOUT)

    # Prepare the mindmap data summary for the prompt
    # We only need titles and subtopic names for the prompt context
    prompt_context = []
    for mindmap in mindmap_data:
        topic_name = mindmap.get('title', 'Untitled Topic')
        code_lines = get_mindmap_code(mindmap).split('\n')
        subtopics = []
        for line in code_lines:
            # Basic parsing: Skip root, mindmap directive, and empty lines
            clean_line = line.strip()
            if clean_line and not clean_line.startswith('root') and not clean_line.startswith('mindmap'):
                 # Remove potential quotes or markdown formatting for the prompt
                 subtopic_name = re.sub(r'^["\'-]*|["\'-]*$', '', clean_line)
                 if subtopic_name: # Ensure it's not empty after cleaning
                    subtopics.append(subtopic_name)

        if subtopics:
             prompt_context.append({"topic": topic_name, "subtopics": subtopics})
        elif topic_name != 'Untitled Topic': # Add topic even if no subtopics parsed
             prompt_context.append({"topic": topic_name, "subtopics": ["General Overview"]})


    if not prompt_context:
        print("Error: Could not extract any topics/subtopics from mindmap data.")
        raise ValueError("Could not extract structure from mindmap data for prompt.")

    # Construct the single, comprehensive prompt

    try:
        print(f"Generating study plan with quizzes for {len(prompt_context)} topics...")
        start_time = time.time()

        response = call_gemini_with_rate_limit_retry(
            client.chat.completions.create,
            operation_name="Study plan generation",
            model=get_model_for("study_plan"),
            messages=[{
                "role": "system",
                "content": "You are a helpful AI that creates study plans."
            }, {
                "role": "user",
                "content": f"""Generate a study plan for this topic. Include:
                - Main topic and subtopics
                - Estimated study time in minutes (for reference)
                - 5 multiple-choice questions per subtopic and the questions should be mandatorily present in the mindmap data.
                
                Format as JSON with structure:
                {{
                  "study_plan": [
                    {{
                      "topic": "[Title]",
                      "duration_minutes": "[Time]",
                      "subtopics": [
                        {{
                          "name": "[Subtopic Name]",
                          "duration_minutes": "[Time]",
                          "quiz": [
                            {{
                              "question": "[Question Text]",
                              "options": ["[Option 1]", "[Option 2]", "[Option 3]", "[Option 4]"],
                              "answer": "[Correct Option Index]"
                            }}
                          ]
                        }}
                      ]
                    }}
                  ]
                }}

                Use this mindmap content to create the plan and text:
                {json.dumps(prompt_context, indent=2)}, {pdf_text}"""
            }],
            temperature=0.3, # Lower temperature for more predictable structure
            max_tokens=4000, # Adjust as needed, might need more for large plans
            # Explicitly request JSON output if the API/model supports it
            # Note: Check whether the selected Gemini model supports this
            # response_format={"type": "json_object"}
        )

        end_time = time.time()
        print(f"API call completed in {end_time - start_time:.2f} seconds.")

        response_content = response.choices[0].message.content

        # Parse the JSON response carefully
        study_data = parse_json_from_response(response_content)
        print(study_data)

        if not study_data or 'study_plan' not in study_data or not isinstance(study_data['study_plan'], list):
            print("Error: Failed to parse valid study plan JSON from API response.")
            print(f"Raw Response Snippet: {response_content[:500]}...")
            # If parsing fails, create a fallback structure
            study_data = {"study_plan": []}
            for mindmap in mindmap_data:
                 topic_name = mindmap.get('title', 'Fallback Topic')
                 study_data["study_plan"].append({
                     "topic": topic_name,
                     "duration_minutes": 30,
                     "subtopics": [{
                         "name": "General Overview",
                         "duration_minutes": 30,
                         "quiz": create_fallback_quiz(topic_name, "General Overview")
                     }]
                 })
            print("Generated fallback study plan structure.")

        else:
             # Validate structure and add fallback quizzes if any are missing/invalid
             print("Successfully parsed study plan JSON. Validating structure...")
             for topic in study_data.get("study_plan", []):
                 topic_name = topic.get("topic", "Unnamed Topic")
                 if not topic.get("subtopics"): # Ensure subtopics list exists
                     topic["subtopics"] = []
                 for subtopic in topic.get("subtopics", []):
                     subtopic_name = subtopic.get("name", "Unnamed Subtopic")
                     # Check if quiz is missing, empty, or not a list
                     if not isinstance(subtopic.get("quiz"), list) or not subtopic.get("quiz"):
                         print(f"Warning: Missing or invalid quiz for {topic_name} -> {subtopic_name}. Adding fallback.")
                         subtopic["quiz"] = create_fallback_quiz(topic_name, subtopic_name)
                     else:
                         # Optional: Add more validation for individual questions if needed
                         pass
             print("Study plan validation complete.")


        return study_data

    except Exception as e:
        # Catch potential timeouts or other API errors
        print(f"Error during study plan generation API call: {e}")
        # Create a fallback structure on error
        study_data = {"study_plan": []}
        for mindmap in mindmap_data:
             topic_name = mindmap.get('title', 'Fallback Topic on Error')
             study_data["study_plan"].append({
                 "topic": topic_name,
                 "duration_minutes": 30,
                 "subtopics": [{
                     "name": "General Overview",
                     "duration_minutes": 30,
                     "quiz": create_fallback_quiz(topic_name, "General Overview")
                 }]
             })
        print("Generated fallback study plan structure due to API error.")
        return study_data


# --- Main Execution / Testing ---
if __name__ == "__main__":
    print("Running streaks.py test...")

    # Use the same test data as before
    test_data = [{'title': 'Historiography: Development in the West', 'code': 'mindmap\nroot((Historiography: Development in the West))\n  Tradition of Historiography\n    Writing of critical historical narrative known as historiography\n    Historian\'s inclusion depends on conceptual framework\n    Ancient societies used cave paintings, storytelling, songs, ballads\n    Traditional means as sources of history in modern historiography\n  Modern Historiography\n    Four main characteristics\n      Based on scientific principles starting with relevant questions\n      Anthropocentric questions about deeds of ancient human societies\n      Answers supported by reliable evidence\n      Presents mankind\'s journey through past human deeds\n    Roots in ancient Greek writings\n    Herodotus, Greek historian, first used term "History"\n  Development of Scientific Perspective in Europe and Historiography\n    Progress in Philosophy and Science by 18th century\n    Belief in studying social and historical truths scientifically\n    Shift from Divine phenomena to objective history\n    1737: Gottingen University founded with independent history department\n  Notable Scholars\n    René Descartes 1596-1650\n      Emphasized verifying reliability of historical documents\n      Rule: accept nothing true until all doubt excluded\n    Voltaire 1694-1778\n      Included social traditions, trade, economy, agriculture in history\n      Founder of modern historiography\n    Georg Wilhelm Friedrich Hegel 1770-1831\n      Historical reality presented logically\n      Timeline indicates progress\n      History presentation changes with new evidence\n      Developed Dialectics: Thesis, Antithesis, Synthesis\n    Leopold von Ranké 1795-1886\n      Critical method of historical research\n      Emphasis on original documents and careful examination\n      Criticized imaginative narration\n    Karl Marx 1818-1883\n      History as history of class struggle\n      Human relationships shaped by means of production and class inequality\n      "Das Kapital" as key work\n    Annales School\n      Emerged early 20th century France\n      Expanded history beyond politics to climate, agriculture, trade, social divisions\n    Feminist Historiography\n      Restructuring history from women\'s perspective\n      Influenced by Simone de Beauvoir\n      Focus on women\'s employment, family life, social roles\n      Women portrayed as independent social class post-1990\n    Michel Foucault 1926-1984\n      Argued against chronological ordering of history\n      Focused on explaining transitions in history\n      Introduced "archaeology of knowledge"\n      Analyzed neglected areas: psychological disorders, medicine, prisons\n  Historical Research Method\n    Formulating relevant questions\n    Anthropocentric focus on human deeds\n    Supported by reliable evidence\n    Use of interdisciplinary methods: Archaeology, Epigraphy, Linguistics, Numismatics, Genealogy\n    Critical examination of sources\n    Writing historical narrative\n    Comparative analysis and understanding conceptual frameworks\n    Formulating hypotheses'}, {'title': 'Exercises and Questions on Historiography', 'code': 'mindmap\nroot((Exercises and Questions on Historiography))\n  Multiple Choice Questions\n    Founder of modern historiography: Voltaire\n    Author of "Archaeology of Knowledge": Michel Foucault\n  Identify Wrong Pair\n    Hegel - "Reason in History"\n    Ranké - "The Theory and Practice of History"\n    Herodotus - "The Histories"\n    Karl Marx - "Discourse on the Method" Wrong\n  Explain Concepts\n    Dialectics\n      Understanding events through opposites: Thesis, Antithesis, Synthesis\n    Annales School\n      History includes politics, climate, agriculture, trade, social psychology\n  Explain with Reason\n    Focus on women\'s life in historical research\n      Feminist historiography rethinks male-dominated history, includes women\'s roles\n    Foucault\'s "archaeology of knowledge"\n      Emphasizes explaining historical transitions over chronological truth\n  Concept Chart\n    Notable Scholars in Europe\n      René Descartes\n      Voltaire\n      Hegel\n      Leopold von Ranké\n      Karl Marx\n      Annales School\n      Feminist Historiography\n      Michel Foucault\n  Detailed Answers\n    Karl Marx\'s Class Theory\n      History is class struggle due to unequal access to means of production\n    Four Characteristics of Modern Historiography\n      Scientific principles, anthropocentric questions, evidence-based answers, human deeds graph\n    Feminist Historiography\n      Restructuring history from women\'s perspective, inclusion and rethinking male bias\n    Leopold von Ranké’s Perspective\n      Critical method, original documents, rejection of imaginative narration\n  Project Ideas\n    Write history of a favorite subject\n      Examples: History of Pen, Printing Technology, Computers'}, {'title': 'Historical Research Method and Its Features', 'code': "mindmap\nroot((Historical Research Method and Its Features))\n  Scientific Principles\n    Begins with formation of relevant questions\n    Questions are anthropocentric about human deeds\n    Answers supported by reliable evidence\n    Presents mankind's journey graphically\n  Interdisciplinary Methods Used\n    Archaeology\n    Archival Science\n    Manuscriptology\n    Epigraphy study of inscriptions\n    Lettering style analysis\n    Linguistics\n    Numismatics study of coins\n    Genealogy study of lineage\n  Process of Historical Research\n    Collect historical information\n    Highlight processes leading to historical transitions\n    Carry out comparative analysis\n    Understand time, space, and conceptual frameworks\n    Formulate relevant questions and hypotheses\n    Critically examine sources\n    Write historical narrative"}, {'title': 'Tradition and Modern Historiography', 'code': "mindmap\nroot((Tradition and Modern Historiography))\n  Tradition of Historiography\n    Writing of critical historical narrative\n    Historian's conceptual framework influences narrative\n    Ancient societies lacked formal historiography\n    Used cave paintings, storytelling, songs as history sources\n  Modern Historiography Characteristics\n    Scientific method based\n    Anthropocentric questions\n    Evidence-based answers\n    Human deeds as history graph\n    Rooted in ancient Greek writings Herodotus"}]

    try:
        print("\nAttempting to generate study plan with the new function...")
        study_data = generate_study_plan_and_quizzes(test_data)

        print("\n--- Generated Study Data ---")
        # Pretty print the JSON output
        print(json.dumps(study_data, indent=2))

        # Basic validation check
        if study_data and 'study_plan' in study_data and study_data['study_plan']:
            print("\nTest Result: Successfully generated study plan data.")
            first_topic = study_data['study_plan'][0]
            if first_topic.get('subtopics') and first_topic['subtopics'][0].get('quiz'):
                 print("Structure seems valid (found topics, subtopics, and quizzes).")
            else:
                 print("Warning: Generated data might be missing expected structure (subtopics/quizzes).")
        else:
            print("\nTest Result: Failed to generate valid study plan data or plan is empty.")

    except ValueError as ve:
        print(f"\nTest Error: {ve}")
    except ConnectionError as ce:
         print(f"\nTest Error: {ce}")
    except Exception as e:
        print(f"\nAn unexpected error occurred during the test: {e}")

