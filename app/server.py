from flask import Flask, render_template, jsonify, request
import re
import sqlite3
import openai
from pdfminer.high_level import extract_text
from flask_cors import CORS
from functools import lru_cache

from config import load_config
from db.queries import JOB_FLAGS, read_job, read_ranked_jobs, save_generated_text, update_job_flags
from db.schema import init_db
from app.job_match import job_match, load_profiles

config = load_config('config.json')
TABLE_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def jobs_table():
    # config['jobs_tablename'] (default 'jobs'), checked because it is written into SQL
    name = config.get('jobs_tablename', 'jobs')
    if not isinstance(name, str) or not TABLE_NAME.match(name):
        raise ValueError(f"Invalid jobs_tablename in config: {name!r}")
    return name

app = Flask(__name__)
CORS(app)
app.config['TEMPLATES_AUTO_RELOAD'] = True

def read_pdf(file_path):
    try:
        text = extract_text(file_path)
        return text
    except FileNotFoundError:
        print(f"Error: The file '{file_path}' was not found.")
        return None
    except Exception as e:
        print(f"An error occurred while reading the PDF: {e}")
        return None

@app.route('/')
def home():
    jobs = read_jobs_from_db()
    return render_template('jobs.html', jobs=jobs)

@app.route('/jobs/<int:job_id>')
def job(job_id):
    job = next((job for job in read_jobs_from_db() if job['id'] == job_id), None)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    return render_template('job_description.html', job=job)

@app.route('/jobs')
def get_all_jobs():
    return jsonify(read_jobs_from_db())

@app.route('/jobs/<int:job_id>/details')
def job_details(job_id):
    conn = sqlite3.connect(config["db_path"])
    try:
        job = read_job(conn, jobs_table(), job_id)
    finally:
        conn.close()
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    job['match'] = read_job_match(job_id)
    return jsonify(job)

def job_flags(payload):
    """The {flag: 0|1} updates in a PATCH body, e.g. {"applied": 1}; raises ValueError for anything else."""
    if not isinstance(payload, dict) or not payload:
        raise ValueError(f"Send a JSON object with one or more of: {', '.join(JOB_FLAGS)}")
    unknown = sorted(set(payload) - set(JOB_FLAGS))
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(unknown)}; allowed: {', '.join(JOB_FLAGS)}")
    bad = sorted(name for name, value in payload.items() if value not in (0, 1) or isinstance(value, float))
    if bad:
        raise ValueError(f"Values must be 0 or 1: {', '.join(bad)}")
    return {name: int(value) for name, value in payload.items()}

@app.route('/jobs/<int:job_id>', methods=['PATCH'])
def update_job(job_id):
    # Set applied / hidden / interview / rejected, e.g. {"hidden": 1}; 0 undoes a flag
    try:
        flags = job_flags(request.get_json(silent=True))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    conn = sqlite3.connect(config["db_path"])
    try:
        found = update_job_flags(conn, jobs_table(), job_id, flags)
    finally:
        conn.close()
    if not found:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"id": job_id, "updated": flags}), 200

@app.route('/jobs/<int:job_id>/cover-letter', methods=['GET'])
def get_cover_letter(job_id):
    conn = sqlite3.connect(config["db_path"])
    try:
        job = read_job(conn, jobs_table(), job_id, ['cover_letter'])
    finally:
        conn.close()
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"cover_letter": job['cover_letter']})

@app.route('/jobs/<int:job_id>/resume', methods=['POST'])
def get_resume(job_id):
    print("Resume clicked!")
    conn = sqlite3.connect(config["db_path"])
    job = read_job(conn, jobs_table(), job_id, ['job_description', 'title', 'company'])
    if job is None:
        conn.close()
        return jsonify({"error": "Job not found"}), 404
    resume = read_pdf(config["resume_path"])

    # Check if OpenAI API key is empty
    if not config["OpenAI_API_KEY"]:
        print("Error: OpenAI API key is empty.")
        return jsonify({"error": "OpenAI API key is empty."}), 400

    openai.api_key = config["OpenAI_API_KEY"]
    consideration = ""
    user_prompt = ("You are a career coach with a client that is applying for a job as a " 
                   + job['title'] + " at " + job['company'] 
                   + ". They have a resume that you need to review and suggest how to tailor it for the job. "
                   "Approach this task in the following steps: \n 1. Highlight three to five most important responsibilities for this role based on the job description. "
                   "\n2. Based on these most important responsibilities from the job description, please tailor the resume for this role. Do not make information up. "
                   "Respond with the final resume only. \n\n Here is the job description: " 
                   + job['job_description'] + "\n\n Here is the resume: " + resume)
    if consideration:
        user_prompt += "\nConsider incorporating that " + consideration

    try:
        completion = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "user", "content": user_prompt},
            ],
        )
        response = completion.choices[0].message.content
    except Exception as e:
        print(f"Error connecting to OpenAI: {e}")
        return jsonify({"error": f"Error connecting to OpenAI: {e}"}), 500

    save_generated_text(conn, jobs_table(), job_id, 'resume', response)
    conn.close()
    return jsonify({"resume": response}), 200

@app.route('/jobs/<int:job_id>/cover-letter', methods=['POST'])
def get_CoverLetter(job_id):
    print("CoverLetter clicked!")
    conn = sqlite3.connect(config["db_path"])

    def get_chat_gpt(prompt):
        try:
            completion = openai.ChatCompletion.create(
                model=config["OpenAI_Model"],
                messages=[
                    {"role": "user", "content": prompt},
                ],
            )
            return completion.choices[0].message.content
        except Exception as e:
            print(f"Error connecting to OpenAI: {e}")
            return None

    job = read_job(conn, jobs_table(), job_id, ['job_description', 'title', 'company'])
    if job is None:
        conn.close()
        return jsonify({"error": "Job not found"}), 404

    resume = read_pdf(config["resume_path"])

    # Check if resume is None
    if resume is None:
        print("Error: Resume not found or couldn't be read.")
        return jsonify({"error": "Resume not found or couldn't be read."}), 400

    # Check if OpenAI API key is empty
    if not config["OpenAI_API_KEY"]:
        print("Error: OpenAI API key is empty.")
        return jsonify({"error": "OpenAI API key is empty."}), 400

    openai.api_key = config["OpenAI_API_KEY"]
    consideration = ""
    user_prompt = ("You are a career coach with over 15 years of experience helping job seekers land their dream jobs in tech. You are helping a candidate to write a cover letter for the below role. Approach this task in three steps. Step 1. Identify main challenges someone in this position would face day to day. Step 2. Write an attention grabbing hook for your cover letter that highlights your experience and qualifications in a way that shows you empathize and can successfully take on challenges of the role. Consider incorporating specific examples of how you tackled these challenges in your past work, and explore creative ways to express your enthusiasm for the opportunity. Put emphasis on how the candidate can contribute to company as opposed to just listing accomplishments. Keep your hook within 100 words or less. Step 3. Finish writing the cover letter based on the resume and keep it within 250 words. Respond with final cover letter only. \n job description: " + job['job_description'] + "\n company: " + job['company'] + "\n title: " + job['title'] + "\n resume: " + resume)
    if consideration:
        user_prompt += "\nConsider incorporating that " + consideration

    response = get_chat_gpt(user_prompt)
    if response is None:
        return jsonify({"error": "Failed to get a response from OpenAI."}), 500

    user_prompt2 = ("You are young but experienced career coach helping job seekers land their dream jobs in tech. I need your help crafting a cover letter. Here is a job description: " + job['job_description'] + "\nhere is my resume: " + resume + "\nHere's the cover letter I got so far: " + response + "\nI need you to help me improve it. Let's approach this in following steps. \nStep 1. Please set the formality scale as follows: 1 is conversational English, my initial Cover letter draft is 10. Step 2. Identify three to five ways this cover letter can be improved, and elaborate on each way with at least one thoughtful sentence. Step 4. Suggest an improved cover letter based on these suggestions with the Formality Score set to 7. Avoid subjective qualifiers such as drastic, transformational, etc. Keep the final cover letter within 250 words. Please respond with the final cover letter only.")
    if user_prompt2:
        response = get_chat_gpt(user_prompt2)
        if response is None:
            return jsonify({"error": "Failed to get a response from OpenAI."}), 500

    save_generated_text(conn, jobs_table(), job_id, 'cover_letter', response)
    conn.close()
    return jsonify({"cover_letter": response}), 200

def read_jobs_from_db():
    # Visible jobs, latest posting date first, then overall_score (see db.read_ranked_jobs)
    conn = sqlite3.connect(config["db_path"])
    try:
        return read_ranked_jobs(conn, jobs_table())
    finally:
        conn.close()

@lru_cache(maxsize=1)
def match_profiles():
    # Loaded once per app start; restart the app after changing resume.json or skills_flat.json
    profiles = load_profiles(config.get('resume_json_path', 'resume.json'), config.get('skills_path', 'skills_flat.json'))
    for error in profiles['errors']:
        print(f"Keyword matching disabled: {error}")
    return profiles

def read_job_match(job_id):
    # Matched keywords and gaps for the job page (see job_match.py)
    conn = sqlite3.connect(config["db_path"])
    try:
        return job_match(conn, job_id, match_profiles())
    finally:
        conn.close()

def verify_db_schema():
    # Create missing tables and add missing columns (cover_letter, resume, the ATS scores, ...); see schema.py
    conn = sqlite3.connect(config["db_path"])
    try:
        init_db(conn, jobs_table())
    finally:
        conn.close()

def main():
    verify_db_schema()  # Verify the DB schema before running the app
    app.run(debug=True, port=5001)


if __name__ == "__main__":
    main()
