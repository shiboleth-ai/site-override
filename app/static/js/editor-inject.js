/**
 * Visual editor overlay - injected into cloned site pages.
 * Provides click-to-edit functionality for QA content editing.
 */
(function() {
    'use strict';

    const domain = document.querySelector('meta[name="site-override-domain"]')?.content;
    const filePath = document.querySelector('meta[name="site-override-path"]')?.content;
    if (!domain || !filePath) return;

    let editMode = false;

    // ── Create Toolbar ──
    const toolbar = document.createElement('div');
    toolbar.id = 'so-toolbar';
    toolbar.innerHTML = `
        <span class="so-label">Editing: <strong>${filePath}</strong></span>
        <button class="so-btn-edit" id="so-toggle-edit">Click to Edit</button>
        <button class="so-btn-save" id="so-save" style="display:none !important">Save Changes</button>
        <span class="so-status" id="so-status"></span>
    `;
    document.body.prepend(toolbar);
    document.body.classList.add('so-editor-active');

    const toggleBtn = document.getElementById('so-toggle-edit');
    const saveBtn = document.getElementById('so-save');
    const statusEl = document.getElementById('so-status');

    // ── Toggle Edit Mode ──
    toggleBtn.addEventListener('click', function() {
        editMode = !editMode;

        if (editMode) {
            document.body.classList.add('so-editing');
            toggleBtn.textContent = 'Stop Editing';
            toggleBtn.classList.add('active');
            saveBtn.style.display = 'inline-flex';

            // Make text elements editable
            enableEditing();
        } else {
            document.body.classList.remove('so-editing');
            toggleBtn.textContent = 'Click to Edit';
            toggleBtn.classList.remove('active');
            saveBtn.style.display = 'none';

            // Remove contenteditable
            disableEditing();
        }
    });

    // ── Save ──
    saveBtn.addEventListener('click', function() {
        statusEl.textContent = 'Saving...';

        // Get the page HTML
        const html = document.documentElement.outerHTML;

        const formData = new FormData();
        formData.append('path', filePath);
        formData.append('html', html);

        fetch(`/editor/${domain}/visual-save`, {
            method: 'POST',
            body: formData,
        })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'saved') {
                statusEl.textContent = 'Saved!';
                setTimeout(() => { statusEl.textContent = ''; }, 2000);
            } else {
                statusEl.textContent = 'Error saving';
                statusEl.style.color = '#f87171';
            }
        })
        .catch(() => {
            statusEl.textContent = 'Error saving';
            statusEl.style.color = '#f87171';
        });
    });

    // ── Editable Elements ──
    const EDITABLE_TAGS = [
        'P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
        'SPAN', 'A', 'LI', 'TD', 'TH', 'LABEL',
        'BUTTON', 'STRONG', 'EM', 'B', 'I', 'U',
        'BLOCKQUOTE', 'FIGCAPTION', 'CITE', 'SMALL',
        'DIV', 'SECTION', 'ARTICLE',
    ];

    function enableEditing() {
        // Only make leaf text elements editable (elements with direct text content)
        const walker = document.createTreeWalker(
            document.body,
            NodeFilter.SHOW_ELEMENT,
            {
                acceptNode: function(node) {
                    // Skip our toolbar
                    if (node.id === 'so-toolbar' || node.closest('#so-toolbar')) {
                        return NodeFilter.FILTER_REJECT;
                    }
                    if (!EDITABLE_TAGS.includes(node.tagName)) {
                        return NodeFilter.FILTER_SKIP;
                    }
                    // Check if element has direct text content (not just child elements)
                    const hasText = Array.from(node.childNodes).some(
                        child => child.nodeType === Node.TEXT_NODE && child.textContent.trim()
                    );
                    if (hasText) {
                        return NodeFilter.FILTER_ACCEPT;
                    }
                    return NodeFilter.FILTER_SKIP;
                }
            }
        );

        let node;
        while (node = walker.nextNode()) {
            node.setAttribute('contenteditable', 'true');
        }
    }

    function disableEditing() {
        document.querySelectorAll('[contenteditable]').forEach(el => {
            el.removeAttribute('contenteditable');
        });
    }
})();
