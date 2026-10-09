var selectedJob = null;

async function showJobDetails(jobId) {
    if (selectedJob !== null) {
        selectedJob.classList.remove('job-item-selected');
    }
    console.log('Showing job details: ' + jobId); 
    var newSelectedJob = document.querySelector(`.job-item[data-job-id="${jobId}"]`);
    newSelectedJob.classList.add('job-item-selected');
    selectedJob = newSelectedJob;

    const response = await fetch('/job_details/' + jobId);
    const jobData = await response.json();

    updateJobDetails(jobData);

    if ('cover_letter' in jobData) {
        updateCoverLetter(jobData.cover_letter);
    } else {
        updateCoverLetter(null);
    }
}





function updateCoverLetter(coverLetter) {
    var coverLetterPane = document.getElementById('cover-letter-pane');
    // Check if the coverLetterPane exists
    if (coverLetterPane) {
        // Check if cover letter exists
        if (coverLetter === null) {
            coverLetterPane.innerText = 'No cover letter exists for this job.';
        } else {
            coverLetterPane.innerText = coverLetter;
        }
    }
}



function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => (
        {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function scoreClass(score) {
    return score >= 70 ? 'score-high' : score >= 40 ? 'score-mid' : 'score-low';
}

function chips(terms, kind) {
    if (!terms || terms.length === 0) {
        return '<span class="match-empty">none</span>';
    }
    return terms.map(term => '<span class="chip chip-' + kind + '">' + escapeHtml(term) + '</span>').join('');
}

function matchSection(title, score, summary, missingNote) {
    // One score with its matched keywords and gaps; summary is null when there is nothing to compare
    var value = score || 0;
    var html = '<div class="match-section">';
    html += '<h4>' + title + ' <span class="score ' + scoreClass(value) + '">' + value + '</span></h4>';
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
    var coverLetterDiv = document.getElementById('bottom-pane'); // Get the cover letter div
    console.log('Updating job details: ' + job.id); // Log the jobId here
    var html = '<h2 class="job-title">' + job.title + '</h2>';
    html += '<div class="button-container" style="text-align:center">';
    html += '<a href="' + job.job_url + '" class="job-button">Go to job</a>';
    html += '<button class="job-button" onclick="markAsCoverLetter(' + job.id + ')">Cover Letter</button>';
    html += '<button class="job-button" onclick="markAsApplied(' + job.id + ')">Applied</button>';
    html += '<button class="job-button" onclick="markAsRejected(' + job.id + ')">Rejected</button>';
    html += '<button class="job-button" onclick="markAsInterview(' + job.id + ')">Interview</button>';
    html += '<button class="job-button" onclick="hideJob(' + job.id + ')">Hide</button>';
    html += '</div>';
    html += '<p class="job-detail">' + job.company + ', ' + job.location + '</p>';
    html += '<p class="job-detail">' + job.date + '</p>';
    var match = job.match || {};
    html += matchSection('ATS score (rules, resume.json)', job.ats_score, match.rule,
                         'No parsed requirements for this job, or resume.json could not be read.');
    html += matchSection('ATS score (embeddings, skills_flat.json)', job.ats_score_embed, match.embed,
                         'No embedder.py matches for this job yet, or skills_flat.json could not be read.');
    html += '<p class="job-description">' + job.job_description + '</p>';

    jobDetailsDiv.innerHTML = html;
    if (job.cover_letter) {
        // Update the cover letter div
        coverLetterDiv.innerHTML = '<p class="job-description">' + job.cover_letter + '</p>';
    } else {
        // Clear the cover letter div if no cover letter exists
        coverLetterDiv.innerHTML = '';
    }
}


function markAsApplied(jobId) {
    console.log('Marking job as applied: ' + jobId)
    fetch('/mark_applied/' + jobId, { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            console.log(data);  // Log the response
            if (data.success) {
                var jobCard = document.querySelector(`.job-item[data-job-id="${jobId}"]`);
                jobCard.classList.add('job-item-applied');
            }
        });
}

function markAsCoverLetter(jobId) {
    console.log('Marking job as cover letter: ' + jobId)
    fetch('/get_CoverLetter/' + jobId, { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            console.log(data);  // Log the response
            if (data.cover_letter) {
                // Show the job details again, this will also update the cover letter
                showJobDetails(jobId);
            }
        });
}

function markAsRejected(jobId) {
    console.log('Marking job as rejected: ' + jobId)
    fetch('/mark_rejected/' + jobId, { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            console.log(data);  // Log the response
            if (data.success) {
                var jobCard = document.querySelector(`.job-item[data-job-id="${jobId}"]`);
                jobCard.classList.add('job-item-rejected');
            }
        });
}

function hideJob(jobId) {
    fetch('/hide_job/' + jobId, { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                var jobCard = document.querySelector(`.job-item[data-job-id="${jobId}"]`);
                
                // Find the next sibling in the DOM that is a job-item
                var nextJobCard = jobCard.nextElementSibling;
                while(nextJobCard && !nextJobCard.classList.contains('job-item')) {
                    nextJobCard = nextJobCard.nextElementSibling;
                }
                
                // If a next job exists, show its details
                if (nextJobCard) {
                    var nextJobId = nextJobCard.getAttribute('data-job-id');
                    showJobDetails(nextJobId);
                }
                
                // Hide the current job
                jobCard.style.display = 'none'; // Or you can remove it from DOM entirely

                // If no next job exists, clear the job details div
                if (!nextJobCard) {
                    var jobDetailsDiv = document.getElementById('job-details');
                    jobDetailsDiv.innerHTML = '';
                }
            }
        });
}


function markAsInterview(jobId) {
    console.log('Marking job as interview: ' + jobId)
    fetch('/mark_interview/' + jobId, { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            console.log(data);  // Log the response
            if (data.success) {
                var jobCard = document.querySelector(`.job-item[data-job-id="${jobId}"]`);
                jobCard.classList.add('job-item-interview');
            }
        });
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