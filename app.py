import streamlit as st
import test_new
import io
import re
import pandas as pd

# Page Configuration
st.set_page_config(
    page_title="Redmine Bulk Uploader",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom Design and Styling
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap');
    
    /* Global Typography */
    html, body, [class*="css"] {
        font-family: 'Outfit', sans-serif;
    }
    
    /* Header Gradient & Style */
    .title-container {
        padding: 1.5rem 0rem;
        background: linear-gradient(135deg, #1f2937, #111827);
        border-radius: 12px;
        text-align: center;
        margin-bottom: 2rem;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
    }
    
    .title-text {
        font-size: 2.8rem;
        font-weight: 700;
        background: linear-gradient(90deg, #ff4b4b 0%, #ff8f8f 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin: 0;
    }
    
    .subtitle-text {
        font-size: 1.1rem;
        color: #9ca3af;
        margin-top: 0.5rem;
    }
    
    /* Premium Styling for Status/Logs */
    .stCode {
        border-radius: 8px;
        border: 1px solid #374151;
    }
</style>
""", unsafe_allow_html=True)

# Application Header
st.markdown("""
<div class="title-container">
    <h1 class="title-text">🚀 Redmine Bulk Task Uploader</h1>
    <p class="subtitle-text">Fast, reliable, and automated task creation using the Redmine REST API</p>
</div>
""", unsafe_allow_html=True)

# Log Sink class to update UI and build a dynamic task status table in real-time
class StreamlitLogSink(list):
    def __init__(self, log_placeholder, progress_placeholder, total_tasks, results_placeholder):
        super().__init__()
        self.log_placeholder = log_placeholder
        self.progress_placeholder = progress_placeholder
        self.total_tasks = total_tasks
        self.results_placeholder = results_placeholder
        self.results_list = []
        self.current_idx = None
        self.current_subject = None
    
    def append(self, item):
        super().append(item)
        # Display the plain-text activity log
        self.log_placeholder.code("\n".join(self))
        
        # 1. Parse processing start: INFO: Processing task X: subject
        proc_match = re.search(r"Processing task (\d+): (.*)", item)
        if proc_match:
            self.current_idx = int(proc_match.group(1))
            self.current_subject = proc_match.group(2)
            
            # Update progress bar
            progress_val = min(self.current_idx / self.total_tasks, 1.0)
            self.progress_placeholder.progress(
                progress_val, 
                text=f"Processing task {self.current_idx} of {self.total_tasks}..."
            )
            return
            
        # 2. Parse created issues: OK: Created issue #XXXX: subject
        created_match = re.search(r"Created issue #(\d+): (.*)", item)
        if created_match:
            self.results_list.append({
                "Row / Task": f"Task {self.current_idx}" if self.current_idx else "—",
                "Subject": self.current_subject or created_match.group(2),
                "Status": "✅ Created",
                "Details": f"Issue #{created_match.group(1)}"
            })
            self.update_results_ui()
            return
            
        # 3. Parse failed issues: ERROR: Failed to create 'subject': error
        failed_match = re.search(r"(Failed to create|Exception creating issue) '(.*?)': (.*)", item)
        if failed_match:
            self.results_list.append({
                "Row / Task": f"Task {self.current_idx}" if self.current_idx else "—",
                "Subject": failed_match.group(2),
                "Status": "❌ Failed",
                "Details": failed_match.group(3)
            })
            self.update_results_ui()
            return
            
        # 4. Parse generic skipping/configuration errors: ERROR: Skipping: reason
        skip_match = re.search(r"Skipping: (.*)", item)
        if skip_match:
            self.results_list.append({
                "Row / Task": f"Task {self.current_idx}" if self.current_idx else "—",
                "Subject": self.current_subject or "—",
                "Status": "❌ Skipped / Failed",
                "Details": skip_match.group(1)
            })
            self.update_results_ui()
            return
            
        # 5. Parse empty task skip: WARN: Row X: Skipping empty task (no subject)
        empty_match = re.search(r"Row (\d+): Skipping empty task \(no subject\)", item)
        if empty_match:
            self.results_list.append({
                "Row / Task": f"Row {empty_match.group(1)}",
                "Subject": "—",
                "Status": "⚠️ Skipped",
                "Details": "No subject specified"
            })
            self.update_results_ui()
            return

    def update_results_ui(self):
        if self.results_list:
            df = pd.DataFrame(self.results_list)
            self.results_placeholder.dataframe(
                df, 
                width="stretch",
                column_config={
                    "Status": st.column_config.TextColumn(
                        "Status",
                        help="Task upload status",
                        width="medium",
                    ),
                    "Row / Task": st.column_config.TextColumn(
                        "Row / Task",
                        width="small",
                    ),
                    "Subject": st.column_config.TextColumn(
                        "Subject",
                        width="large",
                    ),
                    "Details": st.column_config.TextColumn(
                        "Details",
                        width="large",
                    )
                }
            )
            
    def clear(self):
        super().clear()
        self.log_placeholder.empty()
        self.progress_placeholder.empty()
        self.results_placeholder.empty()
        self.results_list.clear()

# Sidebar - Settings and Configuration
with st.sidebar:
    st.markdown("### ⚙️ Configuration")
    
    # User inputs the API Key (only this is configurable as requested)
    api_key = st.text_input(
        "Redmine API Key",
        value="",
        type="password",
        help="Provide your personal API Access Key from Redmine (My Account -> API access key)"
    )

# Main Panel Layout
col1, col2 = st.columns([2, 1])

with col2:
    st.markdown("### 📥 Task Template")
    st.write("Your CSV must contain specific columns to be processed by the backend. Download a sample format below:")
    
    # CSV Template generation
    sample_data = (
        "Tracker,Status,Priority,Subject,Assignee,Target version,Start date,Due date,Estimated time,Parent task,RedmineID\n"
        "Task,New,Low,Example task commercial auto development,Shridhara Devadiga,To be done,05/31/2026,07/03/2026,6,#205389,377\n"
        "Task,New,Low,Example task contracts management development,Atharva Ghorpade,,05/31/2026,07/03/2026,12,,332\n"
    )
    st.download_button(
        label="📄 Download Sample CSV Template",
        data=sample_data,
        file_name="redmine_tasks_template.csv",
        mime="text/csv"
    )
    
    st.markdown("### 💡 Field Guide")
    st.markdown("""
    - **RedmineID**: Numeric Project ID (e.g. `377`) or project identifier slug. If empty, the parent issue's project is used.
    - **Subject**: Title of the task. (Required)
    - **Tracker**: e.g., `Task`, `Bug`, `Feature`
    - **Priority**: e.g., `Low`, `Normal`, `High`
    - **Assignee**: Assignee's login, full name, or first/last name.
    - **Parent task**: ID of parent task (e.g., `#205389` or `205389`).
    """)

with col1:
    st.markdown("### 📂 Upload Tasks CSV")
    uploaded_file = st.file_uploader(
        "Choose a CSV file containing your tasks...",
        type=["csv"],
        help="Upload the task sheet with columns corresponding to Redmine issue fields"
    )
    
    if uploaded_file is not None:
        try:
            # Let's decode and read the CSV first to show a preview
            csv_bytes = uploaded_file.getvalue()
            csv_text = csv_bytes.decode("utf-8-sig")
            preview_df = pd.read_csv(io.StringIO(csv_text))
            
            st.success(f"CSV file '{uploaded_file.name}' loaded successfully! ({len(preview_df)} rows detected)")
            
            with st.expander("🔍 Preview CSV Data", expanded=False):
                st.dataframe(preview_df, width="stretch")
                
            # Submit action
            if st.button("🚀 Start Bulk Upload", type="primary", width="stretch"):
                if not api_key:
                    st.error("Please enter your Redmine API Key in the sidebar before starting!")
                else:
                    # Placeholders for progress and logs
                    progress_placeholder = st.empty()
                    progress_placeholder.info("Initializing connection and preparing tasks...")
                    
                    # Create Tabs for cleaner results visualization
                    tab_results, tab_logs = st.tabs(["📝 Task Execution Results", "📋 Activity Log"])
                    
                    with tab_results:
                        results_placeholder = st.empty()
                        results_placeholder.info("Task results will be shown here as they are processed.")
                        
                    with tab_logs:
                        log_placeholder = st.empty()
                        log_placeholder.info("Detailed backend logs will appear here.")
                    
                    # Instantiate our dynamic log sink
                    total_tasks = len(preview_df)
                    log_sink = StreamlitLogSink(
                        log_placeholder=log_placeholder, 
                        progress_placeholder=progress_placeholder, 
                        total_tasks=total_tasks, 
                        results_placeholder=results_placeholder
                    )
                    
                    # Dynamically inject the user-provided API key into backend
                    test_new.API_KEY = api_key
                    
                    # Run bulk upload
                    result_out = {}
                    csv_stream = io.StringIO(csv_text)
                    csv_stream.name = uploaded_file.name
                    
                    with st.spinner("Processing tasks..."):
                        success = test_new.run_bulk_upload(
                            csv_stream,
                            result_out=result_out,
                            log_lines=log_sink
                        )
                    
                    # Show final results summary banner
                    if success and result_out.get("completed", False):
                        progress_placeholder.success("🎉 Bulk upload completed successfully!")
                        st.balloons()
                    else:
                        progress_placeholder.error(
                            f"❌ Bulk upload failed or completed with errors: {result_out.get('error', 'Check logs below')}"
                        )
                    
                    # Summary metrics
                    st.markdown("### 📊 Upload Summary")
                    m_col1, m_col2, m_col3, m_col4 = st.columns(4)
                    m_col1.metric("Total Rows", result_out.get("total_rows", 0))
                    m_col2.metric("Created (Success)", result_out.get("success", 0))
                    m_col3.metric("Failed", result_out.get("failed", 0))
                    m_col4.metric("Skipped", result_out.get("skipped", 0))
                    
        except Exception as e:
            st.error(f"Error loading or parsing the CSV file: {e}")
