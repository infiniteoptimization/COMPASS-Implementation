let currentSessionId = null;
const API_BASE = '/api';

const chatContainer = document.getElementById('chat-container');
const sessionList = document.getElementById('session-list');
const userInput = document.getElementById('user-input');
const sendBtn = document.getElementById('send-btn');

// --- Initialization ---
document.addEventListener('DOMContentLoaded', () => {
    loadSessions();
});

// --- API Interactions ---

async function loadSessions() {
    const res = await fetch(`${API_BASE}/sessions`);
    const sessions = await res.json();
    renderSessionList(sessions);
}

async function createSession(initialMessage) {
    const res = await fetch(`${API_BASE}/sessions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: initialMessage })
    });
    const data = await res.json();
    return data.id;
}

async function loadMessages(sessionId) {
    currentSessionId = sessionId;
    chatContainer.innerHTML = '';

    document.querySelectorAll('.session-item').forEach(el => el.classList.remove('active'));
    const activeItem = document.querySelector(`.session-item[data-id="${sessionId}"]`);
    if (activeItem) activeItem.classList.add('active');

    const res = await fetch(`${API_BASE}/sessions/${sessionId}/messages`);
    const messages = await res.json();

    messages.forEach(msg => {
        // For historical messages, we assume no logs are stored (just the final markdown)
        appendSimpleMessage(msg.role, msg.content);
    });
    scrollToBottom();
}

// --- UI Logic ---

function renderSessionList(sessions) {
    sessionList.innerHTML = '';
    sessions.forEach(s => {
        const div = document.createElement('div');
        div.className = 'session-item';
        div.dataset.id = s.id;
        div.textContent = s.title;
        div.onclick = () => loadMessages(s.id);
        sessionList.appendChild(div);
    });
}

// Used for loading history (simple text bubbles)
function appendSimpleMessage(role, content) {
    const div = document.createElement('div');
    div.className = `message ${role}`;

    if (role === 'assistant') {
        // Wrap content in the final-answer container structure for consistency
        div.innerHTML = `<div class="final-answer-container">${marked.parse(content)}</div>`;
    } else {
        div.innerHTML = marked.parse(content);
    }

    chatContainer.appendChild(div);
    const welcome = document.querySelector('.welcome-msg');
    if (welcome) welcome.remove();
}

function scrollToBottom() {
    chatContainer.scrollTop = chatContainer.scrollHeight;
}

// --- Streaming UI Builder ---

function createStreamingMessage() {
    const wrapper = document.createElement('div');
    wrapper.className = 'message assistant';

    // 1. Create the Accordion for Thoughts
    const details = document.createElement('details');
    details.className = 'thought-process';
    details.open = true; // Start expanded

    const summary = document.createElement('summary');
    summary.innerHTML = '<span class="status-running">Agent Working...</span>';

    const logsContainer = document.createElement('div');
    logsContainer.className = 'logs-container';

    details.appendChild(summary);
    details.appendChild(logsContainer);

    // 2. Create the Final Answer Container
    const answerContainer = document.createElement('div');
    answerContainer.className = 'final-answer-container';

    wrapper.appendChild(details);
    wrapper.appendChild(answerContainer);

    chatContainer.appendChild(wrapper);
    scrollToBottom();

    return {
        wrapper,
        details,
        summary,
        logsContainer,
        answerContainer
    };
}

// --- Main Chat Logic (Streaming) ---

async function handleSend() {
    const query = userInput.value.trim();
    if (!query) return;

    userInput.value = '';

    // 1. Session Handling
    if (!currentSessionId) {
        currentSessionId = await createSession(query);
        loadSessions();
    }

    // Display User Message
    appendSimpleMessage('user', query);
    scrollToBottom();

    // 2. Prepare Stream UI
    // Create the structure: [ Message [ Details [ Logs ] ] [ Answer ] ]
    const streamUI = createStreamingMessage();
    let accumulatedAnswer = ""; // Buffer for Markdown

    // 3. Start Event Source
    const startTime = Date.now();
    const eventSource = new EventSource(`${API_BASE}/chat_stream?session_id=${currentSessionId}&query=${encodeURIComponent(query)}`);

    eventSource.onmessage = (event) => {
        const data = JSON.parse(event.data);

        if (data.type === 'log') {
            // Append Log Entry
            const entry = document.createElement('div');
            entry.className = 'log-entry';
            entry.innerHTML = `<span class="log-tag">[${data.loop_type}] ${data.role}</span> <span class="log-content">${data.content}</span>`;
            streamUI.logsContainer.appendChild(entry);

            // Auto-scroll the log container
            streamUI.logsContainer.scrollTop = streamUI.logsContainer.scrollHeight;

        } else if (data.type === 'final_answer') {
            // Render Answer
            // If the chunk is small, we could append, but Compass sends the whole block usually or chunks.
            // In the python code: "yield f'### Final Answer...'" usually comes as one big chunk or stream chunks.
            // CompassSystem yields the full answer string at the end usually.

            // If content starts with "###", it's the header.
            // We accumulate just in case, though the current Python yields it fully at the end.
            accumulatedAnswer = data.content;
            streamUI.answerContainer.innerHTML = marked.parse(accumulatedAnswer);

            // 4. Cleanup on Finish
            const endTime = Date.now();
            const duration = ((endTime - startTime) / 1000).toFixed(1);
            // Change Summary Text
            streamUI.summary.innerHTML = `Thought Process (Finished in ${duration}s)`;
            // Collapse the details
            streamUI.details.open = false;

            eventSource.close();
            scrollToBottom();
        } else if (data.type === 'error') {
            const endTime = Date.now();
            const duration = ((endTime - startTime) / 1000).toFixed(1);
            streamUI.answerContainer.innerHTML = `<p style="color:red">Error: ${data.content}</p>`;
            streamUI.summary.innerHTML = `Error Occurred (after ${duration}s)`;
            eventSource.close();
        }
    };

    eventSource.onerror = (err) => {
        const endTime = Date.now();
        const duration = ((endTime - startTime) / 1000).toFixed(1);
        console.error("Stream failed", err);
        streamUI.summary.innerHTML = `Connection Lost (after ${duration}s)`;
        eventSource.close();
    };
}

// --- Event Listeners ---

sendBtn.addEventListener('click', handleSend);
userInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSend();
    }
});
document.getElementById('new-chat-btn').addEventListener('click', () => {
    currentSessionId = null;
    chatContainer.innerHTML = '<div class="welcome-msg">Select a chat or start a new one.</div>';
    document.querySelectorAll('.session-item').forEach(el => el.classList.remove('active'));
});