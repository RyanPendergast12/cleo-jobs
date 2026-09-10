import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from bs4 import BeautifulSoup
from unittest.mock import patch
import jobwatch as jw
import jobwatch_workplace as w
import jobwatch_display as d
import jobwatch_match as m

def job(**kw):
    return jw.Job(source="linkedin",title="Patient Access Representative",company="Example Health",
                  url="https://www.linkedin.com/jobs/view/patient-access-representative-4463168489",
                  native_id="4463168489",**kw)
def soup(html):
    return BeautifulSoup(html,"html.parser")

class WorkplaceTests(unittest.TestCase):
    def test_normalized_labels(self):
        for text,expected in [("On-site","Onsite"),("In person","Onsite"),("Hybrid","Hybrid"),("Remote","Remote"),("Full-time",""),("Remote access","")]:
            self.assertEqual(w.normalize(text),expected)
    def test_scoped_badge(self):
        page=soup('<div class="top-card-layout"><h1>Patient Access Representative</h1><button>On-site</button><button>Full-time</button></div>')
        self.assertEqual(w.from_soup(page,"Patient Access Representative")[0],"Onsite")
    def test_unrelated_recommendation_ignored(self):
        page=soup('<div class="top-card-layout"><h1>Patient Access Representative</h1><button>Full-time</button></div><aside><button>Remote</button><h2>Analyst - On-site</h2></aside>')
        self.assertEqual(w.from_soup(page,"Patient Access Representative")[0],"")
    def test_city_does_not_imply_onsite(self):
        page=soup('<div class="top-card-layout"><h1>Patient Access Representative</h1><span class="topcard__flavor">Las Vegas, NV</span></div>')
        self.assertEqual(w.from_soup(page,"Patient Access Representative")[0],"")
    def test_criteria_not_employment_type(self):
        page=soup('<li class="description__job-criteria-item"><h3 class="description__job-criteria-subheader">Workplace type</h3><span class="description__job-criteria-text">Hybrid</span></li>')
        self.assertEqual(w.from_soup(page)[0],"Hybrid")
        page=soup(str(page).replace("Workplace type","Employment type").replace("Hybrid","Full-time"))
        self.assertEqual(w.from_soup(page)[0],"")
    def test_structured_workplace(self):
        row={"@type":"JobPosting","title":"Patient Access Representative","jobLocationType":"TELECOMMUTE"}
        self.assertEqual(w.from_soup(soup('<script type="application/ld+json">'+json.dumps(row)+'</script>'),"Patient Access Representative")[0],"Remote")
    def test_conflicting_metadata_unknown(self):
        page=soup('<div class="top-card-layout"><h1>Patient Access Representative</h1><button>On-site</button><button>Remote</button></div>')
        self.assertEqual(w.from_soup(page,"Patient Access Representative"),("","Conflicting workplace metadata"))
    def test_explicit_metadata_wins(self):
        j=job(description="Work with remote access tools in hybrid cloud systems.")
        w.apply(j,"Remote","LinkedIn posting badge")
        self.assertEqual(d.work_mode(j),"Remote")
        w.apply(j,"On-site","LinkedIn posting badge")
        self.assertEqual(d.work_mode(j),"Onsite")
    def test_no_cross_repository_override_is_applied(self):
        j=job(location="Boston, MA")
        self.assertFalse(w.apply_override(j,today=date(2026,9,4)))
        self.assertEqual(d.work_mode(j),"Not specified")
    def test_expired_and_other_job_override_ignored(self):
        self.assertFalse(w.apply_override(job(),today=date(2026,10,5)))
        j=job();j.url="https://www.linkedin.com/jobs/view/123"
        self.assertFalse(w.apply_override(j,today=date(2026,9,4)))
    def test_details_extracts_badge_even_with_existing_description(self):
        page='<div class="top-card-layout"><h1 class="topcard__title">Patient Access Representative</h1><button>Hybrid</button></div>'
        response=type("R",(),dict(ok=True,text=page))()
        j=job(description="Existing description.")
        with patch.object(jw,"_get",return_value=response):
            jw._fetch_linkedin_details(j)
        self.assertEqual(j.work_mode,"Hybrid")
        self.assertEqual(j.description,"Existing description.")
    def test_in_person_prose(self):
        self.assertEqual(d.work_mode(job(description="This role is in person.")),"Onsite")
