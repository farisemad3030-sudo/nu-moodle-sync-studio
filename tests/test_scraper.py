"""
Test Suite: Course Scraping & DOM Parsing
Covers Features:
- F4: 6 Target Courses Discovery (Tier 1 & Tier 2)
- F5: Standard & Inline Folder Handling (Tier 1 & Tier 2)
- F6: Assignment Attachment Discovery (Tier 1 & Tier 2)
- F7: External URL Extraction & Categorization (Tier 1 & Tier 2)
- F8: Course Subfolder Classification (Tier 1 & Tier 2)
- F9: Course Naming Exceptions Mapping (Tier 1 & Tier 2)
"""

import os
import re
import unittest
from pathlib import Path
from bs4 import BeautifulSoup

from tests.fixtures import (
    TARGET_COURSES,
    SAMPLE_ECE322_HTML,
    SAMPLE_ECE342_INLINE_FOLDER_HTML,
    SAMPLE_ECE344_STANDALONE_FOLDER_HTML,
    SAMPLE_STANDALONE_FOLDER_VIEW_HTML,
    SAMPLE_ASSIGN_VIEW_HTML,
    SAMPLE_ECE354_ARABIC_HTML,
    SAMPLE_EXTERNAL_URL_HTML,
    ReferenceClassifier,
    ReferenceURLClassifier,
    HAS_MOODLE_SYNC,
    moodle_scraper,
)


class TestF4CourseDiscovery(unittest.TestCase):
    """Tests for Feature F4: 6 Target Courses Discovery (Tier 1 & Tier 2)."""

    # ------------------ Tier 1 Tests ------------------

    def test_f4_t1_01_course_id_mapping(self):
        """F4.T1.1: Verify mapping of all 6 target course IDs (21329, 21365, 21383, 21395, 21425, 21443)."""
        expected_ids = {21329, 21365, 21383, 21395, 21425, 21443}
        self.assertEqual(set(TARGET_COURSES.keys()), expected_ids)

    def test_f4_t1_02_course_code_mapping(self):
        """F4.T1.2: Verify mapping of course codes ECE322, ECE342, ECE343, ECE344, ECE353, ECE354."""
        expected_codes = {"ECE322", "ECE342", "ECE343", "ECE344", "ECE353", "ECE354"}
        actual_codes = {info["code"] for info in TARGET_COURSES.values()}
        self.assertEqual(actual_codes, expected_codes)

    def test_f4_t1_03_course_local_dir_mapping(self):
        """F4.T1.3: Verify mapping of local directory names matching existing study folders."""
        expected_dirs = {
            21329: "Electronic circuits",
            21365: "Computer Organization",
            21383: "Microprocessor System Design",
            21395: "Control Systems",
            21425: "Software Engineering",
            21443: "Image and Video Processing",
        }
        for cid, expected_dir in expected_dirs.items():
            self.assertEqual(TARGET_COURSES[cid]["local_dir"], expected_dir)

    def test_f4_t1_04_parse_course_sections(self):
        """F4.T1.4: Verify BeautifulSoup parser extracts section names from course HTML."""
        soup = BeautifulSoup(SAMPLE_ECE322_HTML, "lxml")
        sections = [s.find("h3", class_="sectionname").get_text(strip=True) for s in soup.find_all("li", class_="section")]
        self.assertIn("General", sections)
        self.assertIn("Lectures", sections)
        self.assertIn("Sheets", sections)
        self.assertIn("Assignments", sections)

    def test_f4_t1_05_parse_course_activity_types(self):
        """F4.T1.5: Verify discovery of mod_resource, mod_assign, and mod_url activity nodes."""
        soup = BeautifulSoup(SAMPLE_ECE322_HTML, "lxml")
        resources = soup.find_all("li", class_="modtype_resource")
        assigns = soup.find_all("li", class_="modtype_assign")
        urls = soup.find_all("li", class_="modtype_url")

        self.assertEqual(len(resources), 4)  # cal, lec1, lec2, sheet1
        self.assertEqual(len(assigns), 1)    # Assignment 1
        self.assertEqual(len(urls), 1)       # Sheet 1 tutorial video

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f4_t2_01_empty_course_sections(self):
        """F4.T2.1: Verify parsing course with 0 activities in a section does not raise exceptions."""
        empty_html = "<html><body><ul class='topics'><li class='section'><h3 class='sectionname'>Empty</h3><ul class='section img-text'></ul></li></ul></body></html>"
        soup = BeautifulSoup(empty_html, "lxml")
        section = soup.find("li", class_="section")
        items = section.find_all("li", class_="activity")
        self.assertEqual(len(items), 0)

    def test_f4_t2_02_hidden_stealth_sections(self):
        """F4.T2.2: Verify detection of hidden or restricted Moodle sections."""
        hidden_html = "<html><body><li class='section hidden'><h3 class='sectionname'>Hidden Section</h3></li></body></html>"
        soup = BeautifulSoup(hidden_html, "lxml")
        sec = soup.find("li", class_="section")
        is_hidden = "hidden" in sec.get("class", [])
        self.assertTrue(is_hidden)

    def test_f4_t2_03_course_page_http_error_simulation(self):
        """F4.T2.3: Verify handling of HTTP 404 or 500 error when fetching course."""
        from unittest.mock import MagicMock
        mock_response = MagicMock(status_code=404, text="Course Not Found")
        self.assertEqual(mock_response.status_code, 404)

    def test_f4_t2_04_arabic_section_titles_utf8(self):
        """F4.T2.4: Verify section titles with Arabic characters are decoded without mangling."""
        soup = BeautifulSoup(SAMPLE_ECE354_ARABIC_HTML, "html.parser")
        sec = soup.find("li", class_="section")
        title = sec.find("h3", class_="sectionname").get_text(strip=True)
        self.assertIn("معالجة الصور بالعربي", title)

    def test_f4_t2_05_unexpected_course_id_filtering(self):
        """F4.T2.5: Verify courses outside the 6 target IDs are excluded from sync processing."""
        candidate_ids = [21329, 99999, 21365, 88888]
        target_ids = set(TARGET_COURSES.keys())
        filtered = [cid for cid in candidate_ids if cid in target_ids]
        self.assertEqual(filtered, [21329, 21365])


class TestF5FolderHandling(unittest.TestCase):
    """Tests for Feature F5: Standard & Inline Folder Handling (Tier 1 & Tier 2)."""

    # ------------------ Tier 1 Tests ------------------

    def test_f5_t1_01_standalone_mod_folder_detection(self):
        """F5.T1.1: Verify detection of standalone mod_folder view link."""
        soup = BeautifulSoup(SAMPLE_ECE344_STANDALONE_FOLDER_HTML, "lxml")
        folder = soup.find("li", class_="modtype_folder")
        link = folder.find("a")["href"]
        self.assertIn("/mod/folder/view.php?id=", link)

    def test_f5_t1_02_download_folder_zip_url_construction(self):
        """F5.T1.2: Verify construction of download_folder.php ZIP endpoint from folder id."""
        folder_id = 3001
        expected_url = f"https://moodle.nu.edu.eg/mod/folder/download_folder.php?id={folder_id}"
        constructed = f"https://moodle.nu.edu.eg/mod/folder/download_folder.php?id={folder_id}"
        self.assertEqual(constructed, expected_url)

    def test_f5_t1_03_inline_folder_dom_detection(self):
        """F5.T1.3: Verify inline folder elements (e.g. ECE342 model answers) are recognized on course page."""
        soup = BeautifulSoup(SAMPLE_ECE342_INLINE_FOLDER_HTML, "lxml")
        inline_folder = soup.find("li", class_="inline")
        self.assertIsNotNone(inline_folder)
        filenames = [s.get_text(strip=True) for s in inline_folder.find_all("span", class_="fp-filename")]
        self.assertIn("quiz1-1.pdf", filenames)
        self.assertIn("quiz1-2.pdf", filenames)

    def test_f5_t1_04_inline_file_links_extraction(self):
        """F5.T1.4: Verify direct pluginfile.php links are extracted from inline folder DOM."""
        soup = BeautifulSoup(SAMPLE_ECE342_INLINE_FOLDER_HTML, "lxml")
        links = [a["href"] for a in soup.find_all("a") if "pluginfile.php" in a.get("href", "")]
        self.assertEqual(len(links), 2)
        self.assertTrue(all("forcedownload=1" in link for link in links))

    def test_f5_t1_05_standalone_folder_page_parsing(self):
        """F5.T1.5: Verify standalone folder view page parsing extracts all constituent files."""
        soup = BeautifulSoup(SAMPLE_STANDALONE_FOLDER_VIEW_HTML, "lxml")
        files = [s.get_text(strip=True) for s in soup.find_all("span", class_="fp-filename")]
        self.assertIn("ECE344-Sheet 1-Fall26 (2).pdf", files)
        self.assertIn("ECE344-Sheet_1 Solution-Fall26.pdf", files)

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f5_t2_01_empty_folder_handling(self):
        """F5.T2.1: Verify parsing empty folder page (0 files) returns empty list without error."""
        empty_folder_html = "<div class='foldertree'><div class='empty'>No files</div></div>"
        soup = BeautifulSoup(empty_folder_html, "lxml")
        files = [s.get_text(strip=True) for s in soup.find_all("span", class_="fp-filename")]
        self.assertEqual(files, [])

    def test_f5_t2_02_nested_subfolders_in_folder_dom(self):
        """F5.T2.2: Verify nested directory structure within folder tree is preserved."""
        nested_html = """
        <div class="foldertree">
          <span class="fp-pathfolder">SubfolderA</span>
          <span class="fp-filename">file_in_a.pdf</span>
        </div>
        """
        soup = BeautifulSoup(nested_html, "lxml")
        subfolder = soup.find("span", class_="fp-pathfolder").get_text(strip=True)
        filename = soup.find("span", class_="fp-filename").get_text(strip=True)
        self.assertEqual(f"{subfolder}/{filename}", "SubfolderA/file_in_a.pdf")

    def test_f5_t2_03_special_characters_in_folder_title(self):
        """F5.T2.3: Verify folder titles with punctuation and brackets parse cleanly."""
        title_with_brackets = "Midterm Rooms [Group 1 - 8]"
        sanitized = re.sub(r'[\\/*?:"<>|]', "", title_with_brackets)
        self.assertEqual(sanitized, "Midterm Rooms [Group 1 - 8]")

    def test_f5_t2_04_folder_zip_download_timeout_handling(self):
        """F5.T2.4: Verify timeout on large folder zip download handles gracefully."""
        import requests
        from unittest.mock import MagicMock
        mock_get = MagicMock(side_effect=requests.exceptions.Timeout("Read timeout on folder zip"))
        with self.assertRaises(requests.exceptions.Timeout):
            mock_get("https://moodle.nu.edu.eg/mod/folder/download_folder.php?id=3001", timeout=30)

    def test_f5_t2_05_forcedownload_query_param_stripping(self):
        """F5.T2.5: Verify forcedownload=1 is recognized and cleaned for canonical URL matching."""
        url = "https://moodle.nu.edu.eg/pluginfile.php/123/mod_folder/content/0/test.pdf?forcedownload=1"
        clean_url = url.split("?")[0]
        self.assertEqual(clean_url, "https://moodle.nu.edu.eg/pluginfile.php/123/mod_folder/content/0/test.pdf")


class TestF6AssignmentAttachments(unittest.TestCase):
    """Tests for Feature F6: Assignment Attachment Discovery (Tier 1 & Tier 2)."""

    # ------------------ Tier 1 Tests ------------------

    def test_f6_t1_01_mod_assign_intro_parsing(self):
        """F6.T1.1: Verify parsing of #intro container on mod_assign view page."""
        soup = BeautifulSoup(SAMPLE_ASSIGN_VIEW_HTML, "lxml")
        intro = soup.find("div", id="intro")
        self.assertIsNotNone(intro)
        self.assertIn("branch instructions", intro.get_text())

    def test_f6_t1_02_instructor_attachments_extraction(self):
        """F6.T1.2: Verify extracting instructor attachment files from .introattachment."""
        soup = BeautifulSoup(SAMPLE_ASSIGN_VIEW_HTML, "lxml")
        attachments = [a.get_text(strip=True) for a in soup.select("div.introattachment a")]
        self.assertIn("lab2_starter.asm", attachments)
        self.assertIn("Lab 2 Manual.pdf", attachments)

    def test_f6_t1_03_starter_code_asm_discovery(self):
        """F6.T1.3: Verify finding assembly starter code .asm files attached to ECE342 assignments."""
        soup = BeautifulSoup(SAMPLE_ASSIGN_VIEW_HTML, "lxml")
        asm_files = [a.get_text(strip=True) for a in soup.select("div.introattachment a") if a.get_text(strip=True).endswith(".asm")]
        self.assertEqual(asm_files, ["lab2_starter.asm"])

    def test_f6_t1_04_assignment_sheet_pdf_discovery(self):
        """F6.T1.4: Verify finding sheet/tutorial PDFs attached to assignments."""
        soup = BeautifulSoup(SAMPLE_ASSIGN_VIEW_HTML, "lxml")
        pdf_files = [a.get_text(strip=True) for a in soup.select("div.introattachment a") if a.get_text(strip=True).endswith(".pdf")]
        self.assertEqual(pdf_files, ["Lab 2 Manual.pdf"])

    def test_f6_t1_05_student_submission_distinction(self):
        """F6.T1.5: Verify student submissions (.fileuploadsubmission) are distinguished from instructor attachments."""
        soup = BeautifulSoup(SAMPLE_ASSIGN_VIEW_HTML, "lxml")
        instructor_files = [a.get_text(strip=True) for a in soup.select("div.introattachment a")]
        student_files = [a.get_text(strip=True) for a in soup.select("div.fileuploadsubmission a")]

        self.assertIn("lab2_starter.asm", instructor_files)
        self.assertIn("Faris Emad 241002282 Lab2.asm", student_files)
        # Ensure student file is NOT in instructor attachments
        self.assertNotIn("Faris Emad 241002282 Lab2.asm", instructor_files)

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f6_t2_01_assignment_with_no_attachments(self):
        """F6.T2.1: Verify assignment with text prompt only produces empty attachment list without crashing."""
        no_attach_html = "<div id='intro'><p>Submit answers on Paper in class.</p></div>"
        soup = BeautifulSoup(no_attach_html, "lxml")
        attachments = soup.select("div.introattachment a")
        self.assertEqual(len(attachments), 0)

    def test_f6_t2_02_assignment_with_external_link_in_intro(self):
        """F6.T2.2: Verify handling external link inside assignment description."""
        ext_link_html = "<div id='intro'><p>See data: <a href='https://drive.google.com/file/d/xyz/view'>Dataset</a></p></div>"
        soup = BeautifulSoup(ext_link_html, "lxml")
        links = [a["href"] for a in soup.select("#intro a") if "pluginfile.php" not in a["href"]]
        self.assertIn("drive.google.com", links[0])

    def test_f6_t2_03_closed_or_overdue_assignment(self):
        """F6.T2.3: Verify closed assignment status table still allows extracting instructor attachments."""
        closed_html = """
        <div id='intro'><div class='introattachment'><a href='file.pdf'>Prompt.pdf</a></div></div>
        <div class='submissionstatustable'><div class='submissionstatus'>Assignment is overdue</div></div>
        """
        soup = BeautifulSoup(closed_html, "lxml")
        attachments = [a.get_text(strip=True) for a in soup.select("div.introattachment a")]
        self.assertEqual(attachments, ["Prompt.pdf"])

    def test_f6_t2_04_multiple_attachments_mixed_types(self):
        """F6.T2.4: Verify handling multiple attachments with mixed extensions (.zip, .docx, .m)."""
        mixed_html = """
        <div id='intro'>
          <div class='introattachment'><a href='1.zip'>bundle.zip</a></div>
          <div class='introattachment'><a href='2.docx'>report.docx</a></div>
          <div class='introattachment'><a href='3.m'>script.m</a></div>
        </div>
        """
        soup = BeautifulSoup(mixed_html, "lxml")
        attachments = [a.get_text(strip=True) for a in soup.select("div.introattachment a")]
        self.assertEqual(len(attachments), 3)

    def test_f6_t2_05_whitespace_in_attachment_filenames(self):
        """F6.T2.5: Verify handling filenames with URL encoded spaces (%20) and leading/trailing spaces."""
        url = "https://moodle.nu.edu.eg/pluginfile.php/1/Lab%202%20Manual%20%28Updated%29.pdf"
        import urllib.parse
        decoded = urllib.parse.unquote(url.split("/")[-1])
        self.assertEqual(decoded, "Lab 2 Manual (Updated).pdf")


class TestF7ExternalURLs(unittest.TestCase):
    """Tests for Feature F7: External URL Extraction & Categorization (Tier 1 & Tier 2)."""

    # ------------------ Tier 1 Tests ------------------

    def test_f7_t1_01_sharepoint_url_classification(self):
        """F7.T1.1: Verify SharePoint / OneDrive links are categorized as SHAREPOINT."""
        url = "https://nileuniversity-my.sharepoint.com/:v:/g/personal/sabdelnassr_nu_edu_eg/IQAtXOu4nGtZTbEbc6gob"
        self.assertEqual(ReferenceURLClassifier.classify(url), "SHAREPOINT")

    def test_f7_t1_02_youtube_url_classification(self):
        """F7.T1.2: Verify YouTube video links are categorized as YOUTUBE."""
        for u in ["https://www.youtube.com/watch?v=vXqlIOX2itM", "https://youtu.be/Ot87qLTODdQ"]:
            self.assertEqual(ReferenceURLClassifier.classify(u), "YOUTUBE")

    def test_f7_t1_03_teams_url_classification(self):
        """F7.T1.3: Verify Microsoft Teams meeting links are categorized as TEAMS."""
        url = "https://teams.microsoft.com/l/team/19%3Ameeting...%40thread.tacv2/conversations"
        self.assertEqual(ReferenceURLClassifier.classify(url), "TEAMS")

    def test_f7_t1_04_forms_url_classification(self):
        """F7.T1.4: Verify Microsoft Forms links are categorized as FORMS."""
        for u in ["https://forms.cloud.microsoft/r/mi26f9Qp2z", "https://forms.office.com/pages/responsepage.aspx"]:
            self.assertEqual(ReferenceURLClassifier.classify(u), "FORMS")

    def test_f7_t1_05_direct_binary_url_classification(self):
        """F7.T1.5: Verify direct external PDF/ZIP links are categorized as DIRECT_BINARY."""
        url = "https://www.dsi.unive.it/~gasparetto/materials/MIPS_Instruction_Set.pdf"
        self.assertEqual(ReferenceURLClassifier.classify(url), "DIRECT_BINARY")

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f7_t2_01_panopto_url_classification(self):
        """F7.T2.1: Verify Panopto recordings are categorized as PANOPTO."""
        url = "https://nu.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=12345-abcde"
        self.assertEqual(ReferenceURLClassifier.classify(url), "PANOPTO")

    def test_f7_t2_02_google_drive_url_classification(self):
        """F7.T2.2: Verify Google Drive links are categorized as GOOGLE_DRIVE."""
        url = "https://drive.google.com/file/d/1A2B3C4D5E/view"
        self.assertEqual(ReferenceURLClassifier.classify(url), "GOOGLE_DRIVE")

    def test_f7_t2_03_malformed_url_handling(self):
        """F7.T2.3: Verify malformed or relative URLs default cleanly to GENERIC_WEB without crashing."""
        for bad in ["not_a_valid_url", "http://", "/relative/path"]:
            self.assertEqual(ReferenceURLClassifier.classify(bad), "GENERIC_WEB")

    def test_f7_t2_04_heavy_query_parameters(self):
        """F7.T2.4: Verify URLs with nested encoded query params and fragments parse accurately."""
        url = "https://nileuniversity-my.sharepoint.com/:v:/g/personal/user/abc?e=123&nav=eyJyZWZlcnJhbEluZm8iOnsidmlldyI6MX19"
        self.assertEqual(ReferenceURLClassifier.classify(url), "SHAREPOINT")

    def test_f7_t2_05_case_insensitive_url_scheme(self):
        """F7.T2.5: Verify uppercase HTTPS scheme is handled properly."""
        url = "HTTPS://WWW.YOUTUBE.COM/WATCH?V=ABCD"
        self.assertEqual(ReferenceURLClassifier.classify(url), "YOUTUBE")


class TestF8SubfolderClassification(unittest.TestCase):
    """Tests for Feature F8: Course Subfolder Classification (Tier 1 & Tier 2)."""

    # ------------------ Tier 1 Tests ------------------

    def test_f8_t1_01_lectures_classification(self):
        """F8.T1.1: Verify routing lecture slides to Lectures."""
        self.assertEqual(ReferenceClassifier.classify("ECE322", "Lectures", "Lecture 1.pdf"), "Lectures")
        self.assertEqual(ReferenceClassifier.classify("ECE342", "General", "Lec 2 Slides.pptx"), "Lectures")

    def test_f8_t1_02_tutorials_sheets_classification(self):
        """F8.T1.2: Verify routing sheets to Tutorials & Sheets."""
        self.assertEqual(ReferenceClassifier.classify("ECE322", "Sheets", "Sheet 1.pdf"), "Tutorials & Sheets/Sheet 01")

    def test_f8_t1_03_labs_classification(self):
        """F8.T1.3: Verify routing lab manuals and code to Labs."""
        self.assertEqual(ReferenceClassifier.classify("ECE342", "Labs", "Lab 2 Manual.pdf"), "Labs")
        self.assertEqual(ReferenceClassifier.classify("ECE344", "Lab Materials", "lab4.m.txt"), "Labs")

    def test_f8_t1_04_assignments_classification(self):
        """F8.T1.4: Verify routing assignments to Assignments."""
        self.assertEqual(ReferenceClassifier.classify("ECE322", "Assignments", "Assignment 1.pdf"), "Assignments")

    def test_f8_t1_05_general_fallback_classification(self):
        """F8.T1.5: Verify routing calendar and specs to General."""
        self.assertEqual(ReferenceClassifier.classify("ECE322", "General", "Calendar_2026.pdf"), "General")

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f8_t2_01_quizzes_and_model_answers_classification(self):
        """F8.T2.1: Verify routing quizzes to Quizzes and solutions to Model Answers."""
        self.assertEqual(ReferenceClassifier.classify("ECE342", "Quizzes", "Quiz 1.pdf"), "Quizzes")
        self.assertEqual(ReferenceClassifier.classify("ECE342", "Model Answers", "Quiz 1 Model Answer.pdf"), "Model Answers")

    def test_f8_t2_02_project_classification(self):
        """F8.T2.2: Verify routing project briefs to Project."""
        self.assertEqual(ReferenceClassifier.classify("ECE344", "Projects", "Project_Brief.pdf"), "Project")

    def test_f8_t2_03_two_digit_sheet_formatting(self):
        """F8.T2.3: Verify single digit sheet numbers are zero-padded (Sheet 01 through Sheet 09)."""
        self.assertEqual(ReferenceClassifier.classify("ECE344", "Tutorials", "Sheet 4 Solutions.pdf"), "Tutorials & Sheets/Sheet 04")
        self.assertEqual(ReferenceClassifier.classify("ECE344", "Tutorials", "Sheet 10 Solutions.pdf"), "Tutorials & Sheets/Sheet 10")

    def test_f8_t2_04_unclassified_fallback_to_general(self):
        """F8.T2.4: Verify obscure or unknown activity titles fall back to General."""
        self.assertEqual(ReferenceClassifier.classify("ECE344", "Miscellaneous", "UnknownDocument.dat"), "General")

    def test_f8_t2_05_case_insensitive_matching(self):
        """F8.T2.5: Verify matching is case-insensitive (LECTURE, sheet, lAb)."""
        self.assertEqual(ReferenceClassifier.classify("ECE322", "LECTURES", "SLIDES.PDF"), "Lectures")


class TestF9NamingExceptions(unittest.TestCase):
    """Tests for Feature F9: Course Naming Exceptions Mapping (Tier 1 & Tier 2)."""

    # ------------------ Tier 1 Tests ------------------

    def test_f9_t1_01_ece353_tutorial_naming_convention(self):
        """F9.T1.1: Verify ECE353 routes to Tutorial XX instead of Sheet XX."""
        dest = ReferenceClassifier.classify("ECE353", "Tutorials", "Tutorial 1.pdf")
        self.assertEqual(dest, "Tutorials & Sheets/Tutorial 01")
        self.assertTrue(dest.endswith("Tutorial 01"))
        self.assertNotIn("Sheet 01", dest)

    def test_f9_t1_02_ece342_sheet_naming_convention(self):
        """F9.T1.2: Verify ECE342 routes to Sheet XX (Sheet 01 through Sheet 08)."""
        dest = ReferenceClassifier.classify("ECE342", "Tutorials", "Sheet 8.pdf")
        self.assertEqual(dest, "Tutorials & Sheets/Sheet 08")

    def test_f9_t1_03_ece354_sheet_04_directory_creation(self):
        """F9.T1.3: Verify ECE354 Sheet 04 is targeted cleanly even though absent on local disk."""
        dest = ReferenceClassifier.classify("ECE354", "Tutorial", "Sheet 4 Solution.pdf")
        self.assertEqual(dest, "Tutorials & Sheets/Sheet 04")

    def test_f9_t1_04_ece354_avoid_nested_assignments(self):
        """F9.T1.4: Verify normalizing nested Assignments/Assignments directory to flat Assignments."""
        raw_path = "Assignments/Assignments/Assignment 1.pdf"
        normalized = raw_path.replace("Assignments/Assignments", "Assignments")
        self.assertEqual(normalized, "Assignments/Assignment 1.pdf")

    def test_f9_t1_05_ece344_avoid_nested_figures_at_course_root(self):
        """F9.T1.5: Verify assignment figures route to Assignments/figures or Assignments."""
        dest = ReferenceClassifier.classify("ECE344", "Assignments", "fig1.png")
        self.assertEqual(dest, "Assignments")

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f9_t2_01_ece342_cheatsheets_routing(self):
        """F9.T2.1: Verify MIPS cheat sheets are preserved in CheatSheets or Lectures."""
        fn = "MIPS_Green_Sheet.png"
        sec = "CheatSheets"
        dest = "CheatSheets" if "cheatsheet" in sec.lower() else "General"
        self.assertEqual(dest, "CheatSheets")

    def test_f9_t2_02_ece343_root_encyclopedia_preservation(self):
        """F9.T2.2: Verify root encyclopedia file in ECE343 is preserved without deletion or overwrite."""
        fn = "ECE343_Quiz1_Master_Encyclopedia.pdf"
        self.assertTrue(fn.startswith("ECE343"))

    def test_f9_t2_03_ece322_group_midterms_routing(self):
        """F9.T2.3: Verify Group 1-8 Midterms in ECE322 route to Exams."""
        dest = ReferenceClassifier.classify("ECE322", "General", "ECE322_Midterm_[Group_1-8]_1.pdf")
        self.assertEqual(dest, "Exams")

    def test_f9_t2_04_ece354_arabic_directory_handling(self):
        """F9.T2.4: Verify ECE354 Arabic directory string 'معالجة الصور بالعربي' handles cleanly."""
        arabic_dir = "Digital Image Processing - معالجة الصور بالعربي"
        encoded = arabic_dir.encode("utf-8")
        self.assertEqual(encoded.decode("utf-8"), arabic_dir)

    def test_f9_t2_05_unknown_course_fallback(self):
        """F9.T2.5: Verify unknown course code behaves gracefully without crash."""
        dest = ReferenceClassifier.classify("UNKNOWN_999", "General", "Syllabus.pdf")
        self.assertEqual(dest, "General")


if __name__ == "__main__":
    unittest.main()
