document.addEventListener('DOMContentLoaded', () => {
    // DOM Elements
    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('file-input');
    const uploadSection = document.getElementById('upload-section');
    const progressSection = document.getElementById('progress-section');
    const progressFilename = document.getElementById('progress-filename');
    const progressFill = document.getElementById('progress-fill');
    const progressPercent = document.getElementById('progress-percent');
    const logsBox = document.getElementById('logs-box');

    const workspaceSection = document.getElementById('workspace-section');
    const blueprintImage = document.getElementById('blueprint-image');
    const prevPageBtn = document.getElementById('prev-page');
    const nextPageBtn = document.getElementById('next-page');
    const pageIndicator = document.getElementById('page-indicator');
    const markdownRender = document.getElementById('markdown-render');
    const downloadBtn = document.getElementById('download-btn');
    const downloadHtmlBtn = document.getElementById('download-html-btn');
    const downloadJsonBtn = document.getElementById('download-json-btn');
    const takeoffInput = document.getElementById('takeoff-input');
    const takeoffBtn = document.getElementById('takeoff-btn');
    const takeoffResults = document.getElementById('takeoff-results');
    const projectDescInput = document.getElementById('project-desc-input');
    const identifyBtn = document.getElementById('identify-btn');
    const identifyStatus = document.getElementById('identify-status');

    const uploadNewHeaderBtn = document.getElementById('upload-new-header-btn');
    const backToUploadBtn = document.getElementById('back-to-upload-btn');

    function showUploadScreen() {
        workspaceSection.classList.add('hidden');
        progressSection.classList.add('hidden');
        uploadSection.classList.remove('hidden');
        if (fileInput) fileInput.value = '';
        if (takeoffResults) takeoffResults.style.display = 'none';
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    if (uploadNewHeaderBtn) uploadNewHeaderBtn.addEventListener('click', showUploadScreen);
    if (backToUploadBtn) backToUploadBtn.addEventListener('click', showUploadScreen);

    // State Variables
    let currentJobId = null;
    let totalPages = 0;
    let currentPage = 1;
    let scratchUrl = '';

    // Project Description Material Identification Handler
    async function runIdentifyMaterial() {
        const descInput = document.getElementById('project-desc-input');
        const idBtn = document.getElementById('identify-btn');
        const idStatus = document.getElementById('identify-status');
        const tInput = document.getElementById('takeoff-input');

        const desc = descInput ? descInput.value.trim() : '';
        if (!desc) {
            if (idStatus) {
                idStatus.style.display = 'block';
                idStatus.style.color = '#f59e0b';
                idStatus.textContent = 'Please enter a project description to identify material.';
            }
            return;
        }

        if (idStatus) {
            idStatus.style.display = 'block';
            idStatus.style.color = '#38bdf8';
            idStatus.textContent = 'Analyzing description and identifying target material...';
        }
        if (idBtn) idBtn.disabled = true;

        try {
            const res = await fetch('/identify-material', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ description: desc, job_id: currentJobId })
            });
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || 'Failed to identify material');
            }
            const data = await res.json();
            if (data && data.matched !== false && data.material) {
                if (tInput) {
                    tInput.value = data.material;
                    tInput.style.borderColor = '#22c55e';
                    tInput.style.boxShadow = '0 0 8px rgba(34, 197, 94, 0.4)';
                    setTimeout(() => {
                        tInput.style.borderColor = '#475569';
                        tInput.style.boxShadow = 'none';
                    }, 2500);
                }

                if (idStatus) {
                    idStatus.style.color = '#22c55e';
                    idStatus.textContent = `✓ Identified Material: "${data.material}". (AI-suggested material — please confirm or edit before proceeding with takeoff)`;
                }
            } else {
                if (idStatus) {
                    idStatus.style.color = '#f59e0b';
                    idStatus.textContent = `⚠️ Could not identify a specific material from your description. Please select a material from the pills below or type it manually.`;
                }
            }
        } catch (err) {
            if (idStatus) {
                idStatus.style.color = '#ef4444';
                idStatus.textContent = `⚠️ Failed to identify material. Please select a material from the quick pills below or enter it manually.`;
            }
        } finally {
            if (idBtn) idBtn.disabled = false;
        }
    }

    window.runIdentifyMaterial = runIdentifyMaterial;


    const idBtn = document.getElementById('identify-btn');
    if (idBtn) {
        idBtn.addEventListener('click', runIdentifyMaterial);
    }
    const pDescInput = document.getElementById('project-desc-input');
    if (pDescInput) {
        pDescInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') runIdentifyMaterial();
        });
    }



    // Quick takeoff global helper
    window.quickTakeoff = function (matName) {
        takeoffInput.value = matName;
        takeoffBtn.click();
    };

    // Load material pills dynamically from the Knowledge Base
    async function loadMaterialPills() {
        const pillsContainer = document.getElementById('material-pills');
        if (!pillsContainer) return;
        try {
            const res = await fetch('/kb-materials');
            if (!res.ok) return;
            const data = await res.json();
            pillsContainer.innerHTML = '';
            (data.materials || []).forEach(mat => {
                const span = document.createElement('span');
                span.className = 'pill-btn';
                span.textContent = mat.standard_name;
                span.onclick = () => quickTakeoff(mat.standard_name);
                pillsContainer.appendChild(span);
            });
        } catch (e) {
            console.warn('Could not load material KB pills:', e);
        }
    }
    loadMaterialPills();

    // Go to specific blueprint page helper
    window.goToPage = function (pageNum) {
        if (pageNum >= 1) {
            currentPage = pageNum;
            updatePageViewer();
            const blueprintContainer = document.querySelector('.blueprint-canvas-container') || document.getElementById('blueprint-image');
            if (blueprintContainer) {
                blueprintContainer.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
        }
    };

    // 1. Drag & Drop Event Listeners
    dropZone.addEventListener('click', () => fileInput.click());

    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('dragover');
    });

    dropZone.addEventListener('dragleave', () => {
        dropZone.classList.remove('dragover');
    });

    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('dragover');
        if (e.dataTransfer.files.length > 0) {
            handleFileUpload(e.dataTransfer.files[0]);
        }
    });

    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            handleFileUpload(e.target.files[0]);
        }
    });

    // 2. Upload and start conversion
    async function handleFileUpload(file) {
        if (!file.name.toLowerCase().endsWith('.pdf')) {
            alert('Please upload a PDF file only.');
            return;
        }

        uploadSection.classList.add('hidden');
        progressSection.classList.remove('hidden');
        progressFilename.textContent = `File: ${file.name}`;
        progressFill.style.width = '0%';
        progressPercent.textContent = '0%';
        logsBox.innerHTML = '';
        addLog('Initialising file upload...');

        const formData = new FormData();
        formData.append('file', file);

        try {
            const response = await fetch('/convert-pdf', {
                method: 'POST',
                body: formData
            });

            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || 'Upload failed');
            }

            const data = await response.json();
            currentJobId = data.job_id;
            addLog(`Upload successful! Job ID: ${currentJobId}`);

            pollJobStatus(currentJobId);
        } catch (error) {
            addLog(`Error: ${error.message}`);
            progressFill.style.backgroundColor = '#ef4444';
            alert(`Failed to start conversion: ${error.message}`);
        }
    }

    // 3. Poll Job Status
    function pollJobStatus(jobId) {
        const interval = setInterval(async () => {
            try {
                const response = await fetch(`/job-status/${jobId}`);
                if (!response.ok) throw new Error('Failed to retrieve job status');

                const job = await response.json();

                progressFill.style.width = `${job.progress}%`;
                progressPercent.textContent = `${job.progress}%`;

                syncLogs(job.logs);

                if (job.status === 'completed') {
                    clearInterval(interval);
                    addLog('Job completed successfully! Launching workspace...');
                    setTimeout(() => loadWorkspace(job), 1000);
                } else if (job.status === 'failed') {
                    clearInterval(interval);
                    progressFill.style.backgroundColor = '#ef4444';
                    addLog(`Job failed: ${job.error || 'Unknown error'}`);
                }
            } catch (error) {
                clearInterval(interval);
                addLog(`Status tracking error: ${error.message}`);
            }
        }, 1500);
    }

    let loggedLinesCount = 0;
    function syncLogs(logsList) {
        if (logsList.length > loggedLinesCount) {
            for (let i = loggedLinesCount; i < logsList.length; i++) {
                addLog(logsList[i]);
            }
            loggedLinesCount = logsList.length;
        }
    }

    function addLog(message) {
        const entry = document.createElement('div');
        entry.className = 'log-entry';
        entry.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
        logsBox.appendChild(entry);
        logsBox.scrollTop = logsBox.scrollHeight;
    }

    // 4. Load side-by-side workspace
    let latestTakeoffJson = null;
    function loadWorkspace(job) {
        if (!job) return;
        uploadSection.classList.add('hidden');
        progressSection.classList.add('hidden');
        workspaceSection.classList.remove('hidden');

        totalPages = job.pages_count || 1;
        currentPage = currentPage || 1;
        scratchUrl = job.scratch_url || '';
        currentJobId = job.job_id || currentJobId;

        updatePageViewer();

        if (downloadJsonBtn) {
            downloadJsonBtn.onclick = () => {
                if (latestTakeoffJson) {
                    const blob = new Blob([JSON.stringify(latestTakeoffJson, null, 2)], { type: 'application/json' });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `takeoff_response_${currentJobId || 'job'}.json`;
                    a.click();
                    URL.revokeObjectURL(url);
                } else {
                    window.location.href = `/download-json/${currentJobId}`;
                }
            };
        }
    }

    // Auto-check for active job on page load
    async function checkActiveJob() {
        try {
            const res = await fetch('/active-job');
            if (res.ok) {
                const job = await res.json();
                if (job && job.scratch_url) {
                    loadWorkspace(job);
                }
            }
        } catch (e) {
            console.log('No active job found', e);
        }
    }
    checkActiveJob();

    // 5. Page Navigation Handler
    async function updatePageViewer() {
        if (!scratchUrl) {
            await checkActiveJob();
        }
        if (scratchUrl && currentPage) {
            blueprintImage.src = `${scratchUrl}/page_${currentPage}.png`;
            blueprintImage.alt = `Blueprint Page ${currentPage}`;
            blueprintImage.style.display = 'block';
        }

        if (pageIndicator) {
            pageIndicator.textContent = totalPages > 0 ? `Page ${currentPage} of ${totalPages}` : `Page ${currentPage}`;
        }

        if (prevPageBtn) prevPageBtn.disabled = (currentPage <= 1);
        if (nextPageBtn) nextPageBtn.disabled = (totalPages > 0 && currentPage >= totalPages);
    }

    prevPageBtn.addEventListener('click', () => {
        if (currentPage > 1) {
            currentPage--;
            updatePageViewer();
        }
    });

    nextPageBtn.addEventListener('click', () => {
        if (totalPages === 0 || currentPage < totalPages) {
            currentPage++;
            updatePageViewer();
        }
    });

    // Keyboard Arrow Navigation
    document.addEventListener('keydown', (e) => {
        if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')) {
            return;
        }
        if (e.key === 'ArrowLeft' && currentPage > 1) {
            currentPage--;
            updatePageViewer();
        } else if (e.key === 'ArrowRight' && (totalPages === 0 || currentPage < totalPages)) {
            currentPage++;
            updatePageViewer();
        }
    });

    // 6. Material Takeoff Handler
    takeoffBtn.addEventListener('click', async () => {
        const query = takeoffInput.value.trim();
        if (!query) {
            alert('Please enter a material name to run takeoff.');
            return;
        }

        if (!currentJobId) {
            alert('Please process a drawing PDF first before running takeoff.');
            return;
        }

        takeoffResults.style.display = 'block';
        takeoffResults.innerHTML = `
            <div style="display: flex; align-items: center; gap: 8px; color: #38bdf8; font-size: 13px;">
                <span class="spinner" style="border: 2px solid #1e293b; border-top: 2px solid #38bdf8; width: 14px; height: 14px; display: inline-block; border-radius: 50%; animation: spin 1s linear infinite;"></span>
                <span>Searching sheets and performing visual measurement for '${query}'...</span>
            </div>
            <style>
                @keyframes spin {
                    0% { transform: rotate(0deg); }
                    100% { transform: rotate(360deg); }
                }
            </style>
        `;
        takeoffBtn.disabled = true;

        try {
            const response = await fetch('/takeoff', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ job_id: currentJobId, material: query, current_page: currentPage })
            });

            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || 'Takeoff request failed');
            }

            const data = await response.json();
            renderTakeoffResults(data);
        } catch (error) {
            takeoffResults.innerHTML = `
                <div style="color: #ef4444; font-size: 13px;">
                    <strong>Error running takeoff:</strong> ${error.message}
                </div>
            `;
        } finally {
            takeoffBtn.disabled = false;
        }
    });

    function renderTakeoffResults(data) {
        latestTakeoffJson = data;
        if (data.scratch_url) scratchUrl = data.scratch_url;
        if (data.pages_count) totalPages = data.pages_count;
        if (data.job_id) currentJobId = data.job_id;

        uploadSection.classList.add('hidden');
        progressSection.classList.add('hidden');
        workspaceSection.classList.remove('hidden');

        if (downloadJsonBtn) {
            downloadJsonBtn.style.display = 'inline-flex';
        }

        if (!data.results || data.results.length === 0) {
            takeoffResults.innerHTML = `
                <div style="color: #94a3b8; font-size: 13px; text-align: center; padding: 10px 0;">
                    No visual measurements found for material: <strong>"${data.material}"</strong>
                </div>
            `;
            return;
        }

        // Auto-navigate left blueprint pane to the primary takeoff page
        const primaryRes = data.results && data.results.length > 0 ? data.results[0] : null;
        if (primaryRes && primaryRes.page_number) {
            currentPage = primaryRes.page_number;
        }
        updatePageViewer();

        let html = `
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 8px;">
                <h4 style="margin: 0; color: #38bdf8; font-size: 15px; font-weight: 600;">Takeoff Results for "${data.material}"</h4>
                <button onclick="copyTakeoffJson()" class="btn btn-secondary" style="font-size: 11px; padding: 4px 10px; display: inline-flex; align-items: center; gap: 4px;">
                    📋 Copy LLM JSON
                </button>
            </div>
        `;

        data.results.forEach(res => {
            html += `
                <div style="margin-bottom: 20px; border-bottom: 1px solid #1e293b; padding-bottom: 15px; text-align: left;">
                    <div style="font-size: 13px; font-weight: 600; color: #e2e8f0; margin-bottom: 8px;">
                        <span>Sheet: ${res.sheet_number} — ${res.sheet_title} (Page ${res.page_number})</span>
                    </div>
            `;

            if (res.error) {
                html += `<div style="color: #ef4444; font-size: 12px;">Error: ${res.error}</div>`;
            } else {
                const tk = res.takeoff;

                // Generic Measurement Table (Material Name & Measured Area/Length - No Costs or Assumptions)
                if (tk.estimation_summary) {
                    const est = tk.estimation_summary;
                    html += `
                        <div style="margin-bottom: 12px; background: #0c1524; border: 1px solid #38bdf8; border-radius: 6px; padding: 12px; font-family: 'Inter', sans-serif;">
                            <div style="font-size: 11px; text-transform: uppercase; color: #38bdf8; font-weight: 700; margin-bottom: 8px;">
                                ${est.category || 'Mesure de Matériau'}
                            </div>
                            <div style="overflow-x: auto;">
                                <table style="width: 100%; border-collapse: collapse; font-size: 13px; text-align: left; color: #e2e8f0;">
                                    <thead>
                                        <tr style="background: #1e293b; color: #94a3b8; font-weight: 600; font-size: 11px;">
                                            <th style="padding: 8px;">Description / Material</th>
                                            <th style="padding: 8px; color: #38bdf8;">Base Measured Quantity</th>
                                            <th style="padding: 8px;">Waste Factor (% Perte)</th>
                                            <th style="padding: 8px; color: #10b981;">Total Takeoff Quantity</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        <tr style="border-top: 1px solid #334155; font-weight: 500;">
                                            <td style="padding: 10px; color: #38bdf8; font-weight: 600;">${est.material_name}</td>
                                            <td style="padding: 10px; font-weight: 700; color: #38bdf8;">${est.info_qty}</td>
                                            <td style="padding: 10px;">${est.waste_percent}</td>
                                            <td style="padding: 10px; color: #10b981; font-weight: 800; font-size: 14px;">${est.total_measured_qty}</td>
                                        </tr>
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    `;
                } else if (tk.takeoff_quantity) {
                    html += `
                        <div style="background: #1e293b; border: 1px solid #334155; border-radius: 6px; padding: 10px; margin-bottom: 12px; display: flex; align-items: center; justify-content: space-between;">
                            <span style="font-size: 12px; color: #94a3b8;">Takeoff Quantity:</span>
                            <span style="font-size: 14px; font-weight: 700; color: #10b981; background: #064e3b; padding: 2px 8px; border-radius: 4px;">${tk.takeoff_quantity}</span>
                        </div>
                    `;
                }

                // Measurement Sources & Origin Breakdown Card
                if (tk.measurement_sources && tk.measurement_sources.length > 0) {
                    html += `
                        <div style="margin-bottom: 14px; background: #091322; border: 1px solid #1e3a8a; border-radius: 8px; padding: 12px;">
                            <div style="font-size: 12px; font-weight: 700; color: #38bdf8; margin-bottom: 10px; display: flex; align-items: center; gap: 6px;">
                                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="16" height="16" style="color: #38bdf8;">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z"/>
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 11a3 3 0 11-6 0 3 3 0 016 0z"/>
                                </svg>
                                <span>Measurement Origin & Detailed Explanation</span>
                            </div>
                            <div style="display: flex; flex-direction: column; gap: 8px;">
                    `;

                    tk.measurement_sources.forEach(src => {
                        html += `
                            <div style="background: #0f172a; border: 1px solid #1e293b; border-radius: 6px; padding: 10px;">
                                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; flex-wrap: wrap; gap: 6px;">
                                    <div style="font-size: 13px; font-weight: 600; color: #e2e8f0; display: flex; align-items: center; gap: 6px;">
                                        <span style="color: #38bdf8;">• ${src.name}:</span>
                                        <span style="color: #10b981; font-weight: 700; background: #064e3b; padding: 2px 6px; border-radius: 4px; font-size: 12px;">${src.value}</span>
                                    </div>
                                    <button onclick="goToPage(${src.source_page})" class="btn-jump-page">
                                        📄 Jump to Page ${src.source_page} (${src.sheet_number})
                                    </button>
                                </div>
                                <div style="font-size: 11px; color: #94a3b8; margin-bottom: 4px;">
                                    <strong>Source Drawing:</strong> Page ${src.source_page} — Sheet ${src.sheet_number}: <em>${src.sheet_title}</em> (${src.drawing_type})
                                </div>
                                <div style="font-size: 12px; color: #cbd5e1; line-height: 1.4; background: #162032; padding: 6px 8px; border-radius: 4px; border-left: 3px solid #38bdf8;">
                                    <strong>How Measured:</strong> ${src.explanation}
                                </div>
                            </div>
                        `;
                    });

                    html += `
                            </div>
                        </div>
                    `;
                }

                if (tk.measurements && tk.measurements.length > 0) {
                    html += `
                        <div style="overflow-x: auto; width: 100%; margin-bottom: 15px;">
                        <table style="width: 100%; border-collapse: collapse; font-size: 12px; background: #0f172a; text-align: left; table-layout: auto;">
                            <thead>
                                <tr style="border-bottom: 1px solid #334155; color: #94a3b8; background: #1e293b;">
                                    <th style="padding: 8px; min-width: 55px; white-space: nowrap;">Value</th>
                                    <th style="padding: 8px; min-width: 40px; white-space: nowrap;">Unit</th>
                                    <th style="padding: 8px; min-width: 120px;">Context / Element</th>
                                    <th style="padding: 8px; min-width: 110px;">Source Page</th>
                                    <th style="padding: 8px; width: 40%;">Detailed Reasoning / How Calculated</th>
                                </tr>
                            </thead>
                            <tbody>
                    `;
                    tk.measurements.forEach(m => {
                        html += `
                            <tr style="border-bottom: 1px solid #1e293b; color: #cbd5e1; vertical-align: top;">
                                <td style="padding: 8px; font-weight: 600; color: #38bdf8; white-space: nowrap;">${m.value}</td>
                                <td style="padding: 8px; white-space: nowrap;">${m.unit}</td>
                                <td style="padding: 8px; color: #94a3b8; font-size: 11px; word-wrap: break-word; overflow-wrap: break-word; white-space: normal;">${m.context || m.type || 'Unknown'}</td>
                                <td style="padding: 8px; word-wrap: break-word; overflow-wrap: break-word;">
                                    ${m.source_page ? `
                                        <button onclick="goToPage(${m.source_page})" class="btn-jump-page" style="font-size: 10px; padding: 2px 6px; white-space: nowrap;">
                                            📄 Page ${m.source_page} (${m.sheet_number || 'N/A'})
                                        </button>
                                    ` : '<span style="color: #475569;">N/A</span>'}
                                </td>
                                <td style="padding: 8px; font-size: 11px; color: #e2e8f0; line-height: 1.4; word-wrap: break-word; overflow-wrap: break-word; white-space: normal;">
                                    ${m.reasoning || m.context || 'Extracted from blueprint visual callouts.'}
                                </td>
                            </tr>
                        `;
                    });
                    html += `
                            </tbody>
                        </table>
                        </div>
                    `;
                }

                if (tk.calculation_formula) {
                    html += `
                        <div style="font-size: 11px; color: #cbd5e1; background: #0c1524; padding: 8px 10px; border-radius: 4px; border-left: 3px solid #10b981; margin-bottom: 8px; line-height: 1.5; white-space: pre-wrap;">
                            <strong>Measurement Calculation Trace:</strong>\n${tk.calculation_formula}
                        </div>
                    `;
                }

                if (tk.visual_evidence) {
                    html += `
                        <div style="font-size: 11px; color: #cbd5e1; background: #0b0f19; padding: 8px 10px; border-radius: 4px; border-left: 3px solid #38bdf8; font-style: italic; line-height: 1.4; margin-bottom: 12px;">
                            <strong>Visual Evidence:</strong> ${tk.visual_evidence}
                        </div>
                    `;
                }

                // Structured LLM Response JSON Viewer Card
                const jsonStr = JSON.stringify(tk, null, 2);
                html += `
                    <div style="margin-top: 15px; background: #020617; border: 1px solid #1e293b; border-radius: 8px; overflow: hidden;">
                        <div style="background: #0f172a; padding: 8px 12px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1e293b;">
                            <span style="font-size: 11px; font-weight: 700; text-transform: uppercase; color: #38bdf8; letter-spacing: 0.5px;">
                                { } LLM Response JSON (Page ${res.page_number})
                            </span>
                            <button onclick="copySingleJson(${res.page_number})" id="copy-btn-${res.page_number}" class="btn btn-secondary" style="font-size: 10px; padding: 2px 8px;">
                                Copy JSON
                            </button>
                        </div>
                        <pre id="json-block-${res.page_number}" style="margin: 0; padding: 12px; font-size: 11px; color: #94a3b8; font-family: 'JetBrains Mono', 'Fira Code', monospace; max-height: 280px; overflow: auto; line-height: 1.4; white-space: pre;">${escapeHtml(jsonStr)}</pre>
                    </div>
                `;
            }

            html += `</div>`;
        });

        takeoffResults.innerHTML = html;
    }

    // Helper functions for JSON copy & escape
    window.copyTakeoffJson = function () {
        if (!latestTakeoffJson) return;
        navigator.clipboard.writeText(JSON.stringify(latestTakeoffJson, null, 2)).then(() => {
            alert('Takeoff response JSON copied to clipboard!');
        }).catch(err => {
            console.error('Copy failed:', err);
        });
    };

    window.copySingleJson = function (pageNum) {
        const block = document.getElementById(`json-block-${pageNum}`);
        const btn = document.getElementById(`copy-btn-${pageNum}`);
        if (!block) return;
        navigator.clipboard.writeText(block.textContent).then(() => {
            if (btn) {
                const orig = btn.textContent;
                btn.textContent = '✓ Copied!';
                setTimeout(() => { btn.textContent = orig; }, 2000);
            }
        });
    };

    function escapeHtml(str) {
        return str
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }
});
