var selectedJob = null;

// API (app/server.py):
//   GET   /jobs/<id>             the job with its keyword matches
//   PATCH /jobs/<id>             {"applied"|"hidden"|"interview"|"rejected": 0|1}
//   POST  /jobs/<id>/cover-letter   generate and save a cover letter

async function requestJson(url, options) {
    // fetch + JSON; throws with the server's error message on a non-2xx response
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(data.error || (response.status + ' ' + response.statusText));
    }
    return data;
}

function updateJob(jobId, fields) {
    return requestJson('/jobs/' + encodeURIComponent(jobId), {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(fields),
    });
}

function jobCard(jobId) {
    return document.querySelector(`.job-item[data-job-id="${CSS.escape(String(jobId))}"]`);
}

async function showJobDetails(jobId) {
    if (selectedJob !== null) {
        selectedJob.classList.remove('job-item-selected');
    }
    var newSelectedJob = jobCard(jobId);
    newSelectedJob.classList.add('job-item-selected');
    selectedJob = newSelectedJob;

    try {
        const job = await requestJson('/jobs/' + encodeURIComponent(jobId));
        updateJobDetails(job);
        updateCoverLetter(job.cover_letter || null);
    } catch (error) {
        document.getElementById('job-details').innerHTML =
            '<p class="job-detail">Could not load this job: ' + escapeHtml(error.message) + '</p>';
    }
}

function updateCoverLetter(coverLetter) {
    var coverLetterPane = document.getElementById('cover-letter-pane');
    if (coverLetterPane) {
        // textContent, not innerHTML: the letter is generated text
        coverLetterPane.textContent = coverLetter === null ? 'No cover letter exists for this job.' : coverLetter;
    }
}

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => (
        {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function scoreClass(score) {
    if (score === null || score === undefined) {
        return 'score-none';
    }
    return score >= 70 ? 'score-high' : score >= 40 ? 'score-mid' : 'score-low';
}

function scoreText(score) {
    // null = not scored yet (no parsed requirements, or a score step hasn't run)
    return (score === null || score === undefined) ? '–' : String(score);
}

function chips(terms, kind) {
    if (!terms || terms.length === 0) {
        return '<span class="match-empty">none</span>';
    }
    return terms.map(term => '<span class="chip chip-' + kind + '">' + escapeHtml(term) + '</span>').join('');
}

function matchSection(title, score, summary, missingNote) {
    // One score with its matched keywords and gaps; summary is null when there is nothing to compare
    var html = '<div class="match-section">';
    html += '<h4>' + title + ' <span class="score ' + scoreClass(score) + '">' + scoreText(score) + '</span></h4>';
    if (summary) {
        html += '<div class="match-row"><strong>Matched:</strong> ' + chips(summary.matched, 'matched') + '</div>';
        html += '<div class="match-row"><strong>Gaps:</strong> ' + chips(summary.gaps, 'gap') + '</div>';
    } else {
        html += '<div class="match-empty">' + missingNote + '</div>';
    }
    return html + '</div>';
}

function updateJobDetails(job) {
    var jobDetailsDiv = document.getElementById('job-details');
    var id = Number(job.id);
    var html = '<h2 class="job-title">' + escapeHtml(job.title) + '</h2>';
    html += '<div class="button-container" style="text-align:center">';
    html += '<a href="' + escapeHtml(job.job_url) + '" class="job-button" target="_blank" rel="noopener">Go to job</a>';
    html += '<button class="job-button" onclick="generateCoverLetter(' + id + ')">Cover Letter</button>';
    html += '<button class="job-button" onclick="markAsApplied(' + id + ')">Applied</button>';
    html += '<button class="job-button" onclick="markAsRejected(' + id + ')">Rejected</button>';
    html += '<button class="job-button" onclick="markAsInterview(' + id + ')">Interview</button>';
    html += '<button class="job-button" onclick="hideJob(' + id + ')">Hide</button>';
    html += '</div>';
    html += '<p class="job-detail">' + escapeHtml(job.company) + ', ' + escapeHtml(job.location) + '</p>';
    html += '<p class="job-detail">' + escapeHtml(job.date) + '</p>';
    var match = job.match || {};
    html += '<p class="job-detail">Overall score <span class="score score-overall ' + scoreClass(job.overall_score) + '">' +
            scoreText(job.overall_score) + '</span> (average of the two ATS scores below)</p>';
    html += matchSection('ATS score (rules, resume.json)', job.ats_score, match.rule,
                         'No parsed requirements for this job, or resume.json could not be read.');
    html += matchSection('ATS score (embeddings, skills_flat.json)', job.ats_score_embed, match.embed,
                         'No embedder.py matches for this job yet, or skills_flat.json could not be read.');
    html += '<p class="job-description" style="white-space: pre-line">' + escapeHtml(job.job_description) + '</p>';
    jobDetailsDiv.innerHTML = html;
}

async function setJobFlag(jobId, flag, cardClass) {
    // PATCH one flag to 1, then colour the job card; failures go to the console
    try {
        await updateJob(jobId, {[flag]: 1});
        jobCard(jobId).classList.add(cardClass);
    } catch (error) {
        console.error('Could not mark job ' + jobId + ' as ' + flag + ': ' + error.message);
    }
}

function markAsApplied(jobId) {
    return setJobFlag(jobId, 'applied', 'job-item-applied');
}

function markAsRejected(jobId) {
    return setJobFlag(jobId, 'rejected', 'job-item-rejected');
}

function markAsInterview(jobId) {
    return setJobFlag(jobId, 'interview', 'job-item-interview');
}

async function generateCoverLetter(jobId) {
    var pane = document.getElementById('cover-letter-pane');
    pane.textContent = 'Generating a cover letter...';
    try {
        const data = await requestJson('/jobs/' + encodeURIComponent(jobId) + '/cover-letter', {method: 'POST'});
        updateCoverLetter(data.cover_letter);
    } catch (error) {
        pane.textContent = 'Could not generate a cover letter: ' + error.message;
    }
}

async function hideJob(jobId) {
    try {
        await updateJob(jobId, {hidden: 1});
    } catch (error) {
        console.error('Could not hide job ' + jobId + ': ' + error.message);
        return;
    }
    var card = jobCard(jobId);
    // Show the next job in the list, if there is one, then hide this one
    var next = card.nextElementSibling;
    while (next && !next.classList.contains('job-item')) {
        next = next.nextElementSibling;
    }
    card.style.display = 'none';
    if (next) {
        showJobDetails(next.getAttribute('data-job-id'));
    } else {
        document.getElementById('job-details').innerHTML = '';
        updateCoverLetter(null);
    }
}

var resizer = document.getElementById('resizer');
var jobDetails = document.getElementById('job-details');
var bottomPane = document.getElementById('bottom-pane');
var originalHeight, originalMouseY;

resizer.addEventListener('mousedown', function(e) {
    e.preventDefault();
    originalHeight = jobDetails.getBoundingClientRect().height;
    originalMouseY = e.pageY;
    document.addEventListener('mousemove', drag);
    document.addEventListener('mouseup', stopDrag);
});

function drag(e) {
    var delta = e.pageY - originalMouseY;
    jobDetails.style.height = (originalHeight + delta) + "px";
    bottomPane.style.height = `calc(100% - ${originalHeight + delta}px - 10px)`;
}

function stopDrag() {
    document.removeEventListener('mousemove', drag);
    document.removeEventListener('mouseup', stopDrag);
}