import calendar
import os
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from dotenv import load_dotenv
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import ai_client
import paypal_client
from ai_client import AIError
from models import (Brand, BrandInterest, Campaign, Influencer, MetricReport, Negotiation, Payment,
                    RoiReport, ShippingInfo, User, db)
from paypal_client import PayPalError

load_dotenv()

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["DATABASE_URL"]
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB uploads
UPLOAD_DIR = os.path.join(app.root_path, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_IMAGES = {"png", "jpg", "jpeg", "webp"}
DEMO_PASSWORD = "demo1234"

db.init_app(app)

STATUS_LABELS = {
    "offered": "Offer sent",
    "negotiating": "Counter-proposal pending",
    "accepted": "Accepted",
    "rejected": "Declined",
    "paid_base": "Funded",
    "content_due": "Content due",
    "reported": "Results submitted",
    "report_ready": "Report ready",
    "bonus_paid": "Bonus paid",
    "completed": "Completed",
}
STATUS_TONE = {
    "offered": "open", "negotiating": "open",
    "accepted": "active", "paid_base": "active", "content_due": "active",
    "reported": "review", "report_ready": "review",
    "bonus_paid": "done", "completed": "done",
    "rejected": "closed",
}
METRIC_LABELS = {"views": "views", "engagements": "engagements (likes + comments)"}


# ---------------------------------------------------------------- helpers
@app.template_filter("money")
def money(value):
    return f"${value:,.2f}"


@app.template_filter("num")
def num(value):
    return f"{value:,}"


def ensure_demo_passwords():
    """Seed users carry the placeholder hash 'x'. Give them a real demo password."""
    changed = False
    for user in User.query.filter_by(password_hash="x"):
        user.password_hash = generate_password_hash(DEMO_PASSWORD)
        changed = True
    if changed:
        db.session.commit()


@app.before_request
def load_user():
    uid = session.get("uid")
    g.user = db.session.get(User, uid) if uid else None


def creator_photo(influencer):
    """Return the URL of static/creators/<user_id>.(jpg|jpeg|png|webp) if the file exists."""
    for ext in ("jpg", "jpeg", "png", "webp"):
        rel = f"creators/{influencer.user_id}.{ext}"
        if os.path.exists(os.path.join(app.static_folder, rel)):
            return url_for("static", filename=rel)
    return None


def brand_photo(brand):
    """Return the URL of static/brands/<user_id>.(jpg|jpeg|png|webp) if the file exists."""
    for ext in ("jpg", "jpeg", "png", "webp"):
        rel = f"brands/{brand.user_id}.{ext}"
        if os.path.exists(os.path.join(app.static_folder, rel)):
            return url_for("static", filename=rel)
    return None


def todo_count():
    """How many tasks need the current user's action (shown as a badge in the menu)."""
    return sum(1 for t in build_tasks(user_campaigns(), g.user.role) if t["kind"] == "action")


@app.context_processor
def inject_globals():
    return {
        "creator_photo": creator_photo,
        "brand_photo": brand_photo,
        "todo_count": todo_count,
        "me": g.get("user"),
        "STATUS_LABELS": STATUS_LABELS,
        "STATUS_TONE": STATUS_TONE,
        "METRIC_LABELS": METRIC_LABELS,
    }


def login_required(role=None):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login"))
            if role and g.user.role != role:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def get_campaign(cid):
    campaign = db.get_or_404(Campaign, cid)
    if g.user.id not in (campaign.brand_id, campaign.influencer_id):
        abort(403)
    return campaign


def to_int(value, minimum=0):
    number = int(value)
    if number < minimum:
        raise ValueError("below minimum")
    return number


def back_to(campaign):
    return redirect(url_for("campaign_detail", cid=campaign.id))


# ------------------------------------------------------------------- auth
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, request.form.get("password", "")):
            session.clear()
            session["uid"] = user.id
            return redirect(url_for("home"))
        flash("The email or password is not correct.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- catalog
@app.route("/")
@login_required()
def home():
    if g.user.role == "influencer":
        return redirect(url_for("brands_feed"))
    niche = request.args.get("niche", "")
    query = Influencer.query
    if niche:
        query = query.filter_by(niche=niche)
    declined, interested = set(), set()
    for row in BrandInterest.query.filter_by(brand_id=g.user.id):
        (declined if row.status == "declined" else interested).add(row.influencer_id)
    influencers = [i for i in query.all() if i.user_id not in declined]
    influencers.sort(key=lambda i: (i.user_id not in interested, -(i.avg_monthly_views or 0)))
    niches = [n for (n,) in db.session.query(Influencer.niche).distinct() if n]
    return render_template("catalog.html", influencers=influencers, interested=interested,
                           niches=sorted(niches), selected=niche)


@app.route("/creators/<int:uid>")
@login_required()
def creator_profile(uid):
    influencer = db.get_or_404(Influencer, uid)
    if g.user.role == "brand" and creator_declined(g.user.id, influencer.user_id):
        flash("This creator is not available for your brand.", "error")
        return redirect(url_for("home"))
    return render_template("influencer.html", inf=influencer)


# -------------------------------------------------------------- campaigns
@app.post("/campaigns")
@login_required("brand")
def create_campaign():
    f = request.form
    influencer = db.get_or_404(Influencer, int(f.get("influencer_id", 0) or 0))
    if creator_declined(g.user.id, influencer.user_id):
        flash("This creator is not available for your brand.", "error")
        return redirect(url_for("home"))
    try:
        videos = to_int(f["videos_count"], 1)
        base = Decimal(f["base_payment"])
        bonus_amount = Decimal(f.get("bonus_amount") or "0")
        threshold = to_int(f.get("bonus_threshold") or "0")
        if base <= 0 or bonus_amount < 0:
            raise ValueError("amounts")
    except (KeyError, ValueError, InvalidOperation):
        flash("Check the numbers in the offer. The base payment must be above zero.", "error")
        return redirect(url_for("creator_profile", uid=influencer.user_id))

    metric = f.get("bonus_metric", "views")
    if metric not in METRIC_LABELS:
        metric = "views"

    campaign = Campaign(
        brand_id=g.user.id,
        influencer_id=influencer.user_id,
        title=f.get("title", "").strip() or f"Campaign with {influencer.display_name}",
        product_name=f.get("product_name", "").strip(),
        product_category=f.get("product_category", "").strip(),
        description=f.get("description", "").strip(),
        videos_count=videos,
        base_payment=base,
        bonus_metric=metric,
        bonus_threshold=threshold,
        bonus_amount=bonus_amount,
        status="offered",
    )
    if not campaign.product_name:
        flash("Add the product name.", "error")
        return redirect(url_for("creator_profile", uid=influencer.user_id))
    db.session.add(campaign)
    db.session.commit()
    flash(f"Offer sent to {influencer.display_name}.", "ok")
    return back_to(campaign)


def parse_month(value):
    """'2026-11' -> date(2026, 11, 1); anything invalid falls back to the current month."""
    try:
        year, month = value.split("-")
        return date(int(year), int(month), 1)
    except (ValueError, AttributeError):
        return date.today().replace(day=1)


def campaign_events(campaigns):
    """Map each calendar day to the dates that matter: start, post deadline, results due."""
    events = {}

    def add(day, kind, text, campaign, done=False):
        events.setdefault(day, []).append(
            {"kind": kind, "text": text, "cid": campaign.id, "done": done})

    for c in campaigns:
        if c.status == "rejected":
            continue
        done = c.report is not None  # results already submitted
        if c.start_date:
            add(c.start_date, "start", f"Starts: {c.title}", c)
        if c.deadline_date:
            add(c.deadline_date, "post",
                f"{'Posted' if done else 'Post by'}: {c.title}", c, done)
            add(c.results_due_date, "results",
                f"{'Results sent' if done else 'Results due'}: {c.title}", c, done)
    return events


@app.route("/campaigns")
@login_required()
def dashboard():
    if g.user.role == "brand":
        query = Campaign.query.filter_by(brand_id=g.user.id)
    else:
        query = Campaign.query.filter_by(influencer_id=g.user.id)
    campaigns = query.order_by(Campaign.id.desc()).all()

    first = parse_month(request.args.get("month", ""))
    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)
    prev_month = (first - timedelta(days=1)).replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    return render_template("dashboard.html", campaigns=campaigns, first=first, weeks=weeks,
                           events=campaign_events(campaigns), today=date.today(),
                           prev_month=prev_month.strftime("%Y-%m"),
                           next_month=next_month.strftime("%Y-%m"))


@app.route("/campaigns/<int:cid>")
@login_required()
def campaign_detail(cid):
    return render_template("campaign.html", c=get_campaign(cid), today=date.today())


# Influencer: accept, decline or counter-propose -----------------------
@app.post("/campaigns/<int:cid>/respond")
@login_required("influencer")
def respond(cid):
    c = get_campaign(cid)
    if c.status != "offered":
        flash("This offer is no longer open.", "error")
        return back_to(c)
    action = request.form.get("action")
    if action == "accept":
        c.status = "accepted"
        flash("Offer accepted. Add your shipping details next.", "ok")
    elif action == "reject":
        c.status = "rejected"
        flash("Offer declined.", "ok")
    elif action == "negotiate":
        try:
            videos = to_int(request.form.get("videos_count"), 1)
        except (ValueError, TypeError):
            flash("Enter a valid number of videos.", "error")
            return back_to(c)
        db.session.add(Negotiation(campaign_id=c.id, proposed_by="influencer",
                                   videos_count=videos,
                                   message=request.form.get("message", "").strip()))
        c.status = "negotiating"
        flash("Counter-proposal sent.", "ok")
    else:
        abort(400)
    db.session.commit()
    return back_to(c)


# Brand: answer a counter-proposal -------------------------------------
@app.post("/campaigns/<int:cid>/counter")
@login_required("brand")
def answer_counter(cid):
    c = get_campaign(cid)
    if c.status != "negotiating" or not c.negotiations:
        flash("There is no counter-proposal to answer.", "error")
        return back_to(c)
    if request.form.get("action") == "accept":
        c.videos_count = c.negotiations[-1].videos_count or c.videos_count
        c.status = "accepted"
        flash("Changes accepted. The campaign is now confirmed.", "ok")
    else:
        c.status = "offered"
        flash("Original terms kept. The creator can accept or decline.", "ok")
    db.session.commit()
    return back_to(c)


# Influencer: shipping details -----------------------------------------
@app.post("/campaigns/<int:cid>/shipping")
@login_required("influencer")
def save_shipping(cid):
    c = get_campaign(cid)
    if c.status not in ("accepted", "paid_base"):
        flash("Shipping details are only needed for accepted offers.", "error")
        return back_to(c)
    f = request.form
    required = ["full_name", "address_line", "city", "country"]
    if any(not f.get(k, "").strip() for k in required):
        flash("Fill in your name, address, city and country.", "error")
        return back_to(c)
    info = c.shipping or ShippingInfo(campaign_id=c.id)
    info.full_name = f["full_name"].strip()
    info.address_line = f["address_line"].strip()
    info.city = f["city"].strip()
    info.postal_code = f.get("postal_code", "").strip()
    info.country = f["country"].strip()
    info.phone = f.get("phone", "").strip()
    info.clothing_size = f.get("clothing_size", "").strip()
    db.session.add(info)
    db.session.commit()
    flash("Shipping details saved.", "ok")
    return back_to(c)


# Brand: product sent, set the content deadline ------------------------
@app.post("/campaigns/<int:cid>/delivered")
@login_required("brand")
def mark_delivered(cid):
    c = get_campaign(cid)
    if c.status != "paid_base" or c.shipping is None:
        flash("Fund the campaign and wait for the creator's shipping details first.", "error")
        return back_to(c)
    try:
        deadline = date.fromisoformat(request.form.get("deadline_date", ""))
        if deadline < date.today():
            raise ValueError("past")
    except ValueError:
        flash("Choose a deadline that is today or later.", "error")
        return back_to(c)
    c.start_date = date.today()
    c.deadline_date = deadline
    c.status = "content_due"
    db.session.commit()
    flash("Product marked as delivered. The deadline is now on the creator's agenda.", "ok")
    return back_to(c)


# Influencer: submit results -------------------------------------------
@app.post("/campaigns/<int:cid>/report")
@login_required("influencer")
def submit_report(cid):
    c = get_campaign(cid)
    if c.status != "content_due":
        flash("Results can be submitted once the product is delivered.", "error")
        return back_to(c)
    try:
        views = to_int(request.form.get("views"))
        likes = to_int(request.form.get("likes"))
        comments = to_int(request.form.get("comments"))
    except (ValueError, TypeError):
        flash("Enter whole numbers for views, likes and comments.", "error")
        return back_to(c)

    shot = request.files.get("screenshot")
    ext = ""
    if shot and shot.filename and "." in shot.filename:
        ext = shot.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_IMAGES:
        flash("Attach a screenshot of your stats (PNG, JPG or WEBP).", "error")
        return back_to(c)
    filename = f"{uuid.uuid4().hex}.{ext}"
    shot.save(os.path.join(UPLOAD_DIR, filename))

    db.session.add(MetricReport(campaign_id=c.id, reported_views=views,
                                reported_likes=likes, reported_comments=comments,
                                screenshot_path=filename))
    c.status = "reported"
    db.session.commit()
    try:
        warnings = run_analysis(c)
        flash("Results submitted and analysed.", "ok")
        for warning in warnings:
            flash(warning, "error")
    except Exception:  # never lose the creator's submission because of the analysis
        app.logger.exception("Analysis failed")
        db.session.rollback()
        flash("Results submitted. The analysis could not run; the brand can retry it.", "ok")
    return back_to(c)


# ------------------------------------------------------------ AI analysis
def _close(declared, seen):
    """True when two numbers differ by at most 5% (or 2 units for tiny numbers)."""
    return abs(declared - seen) <= max(2, 0.05 * max(declared, seen))


def run_analysis(c):
    """1) AI reads the screenshot and we compare it with what the creator reported.
    2) Python computes every number (cost per view, engagement rate, bonus goal).
    3) AI only writes the text. Returns a list of warnings to show the user."""
    report = c.report
    warnings = []
    extracted = None
    try:
        extracted = ai_client.extract_metrics(os.path.join(UPLOAD_DIR, report.screenshot_path))
    except (AIError, OSError) as exc:
        warnings.append(f"The screenshot could not be read automatically ({exc}). Check the numbers by hand.")

    if extracted:
        report.ai_views = extracted["views"]
        report.ai_likes = extracted["likes"]
        report.ai_comments = extracted["comments"]
        diffs = [f"{label}: reported {declared:,}, screenshot shows {seen:,}"
                 for label, declared, seen in (
                     ("views", report.reported_views, extracted["views"]),
                     ("likes", report.reported_likes, extracted["likes"]),
                     ("comments", report.reported_comments, extracted["comments"]))
                 if not _close(declared, seen)]
        report.metrics_match = not diffs
        report.verification_note = ("The reported numbers match the screenshot." if not diffs
                                    else "Mismatch. " + "; ".join(diffs) + ".")
        views, likes, comments = extracted["views"], extracted["likes"], extracted["comments"]
    else:
        report.metrics_match = None
        report.verification_note = "The screenshot could not be read automatically. Review the numbers by hand."
        views, likes, comments = report.reported_views, report.reported_likes, report.reported_comments

    engagements = likes + comments
    measured = views if c.bonus_metric == "views" else engagements
    # The bonus is only earned on numbers the screenshot confirms.
    goal_met = bool(extracted and report.metrics_match and c.bonus_amount > 0
                    and measured >= c.bonus_threshold)
    total_cost = c.base_payment + (c.bonus_amount if goal_met else Decimal("0"))
    cost_per_view = (total_cost / views).quantize(Decimal("0.0001")) if views else None
    cost_per_eng = (total_cost / engagements).quantize(Decimal("0.0001")) if engagements else None
    rate = min(Decimal(engagements) / Decimal(views) * 100, Decimal("999")).quantize(Decimal("0.001")) if views else None

    facts = {
        "campaign": c.title, "product": c.product_name, "category": c.product_category,
        "videos": c.videos_count, "base_payment_usd": float(c.base_payment),
        "bonus_rule": (f"{float(c.bonus_amount)} USD if {c.bonus_metric} reach {c.bonus_threshold}"
                       if c.bonus_amount > 0 else "none"),
        "views": views, "likes": likes, "comments": comments, "engagements": engagements,
        "total_cost_usd": float(total_cost),
        "cost_per_view_usd": float(cost_per_view) if cost_per_view is not None else None,
        "cost_per_engagement_usd": float(cost_per_eng) if cost_per_eng is not None else None,
        "engagement_rate_percent": float(rate) if rate is not None else None,
        "bonus_goal_reached": goal_met,
        "numbers_confirmed_by_screenshot": report.metrics_match,
        "verification_note": report.verification_note,
        "creator_followers": c.influencer.followers,
        "creator_average_monthly_views": c.influencer.avg_monthly_views,
    }
    try:
        text = ai_client.write_report(facts)
    except AIError as exc:
        warnings.append(f"The AI could not write the report text ({exc}). A standard summary was used.")
        text = ai_client.fallback_report(facts)

    roi = c.roi or RoiReport(campaign_id=c.id)
    roi.total_spent = total_cost
    roi.cost_per_view = cost_per_view
    roi.cost_per_engagement = cost_per_eng
    roi.engagement_rate = rate
    roi.bonus_goal_met = goal_met
    roi.summary_text = text["summary"]
    roi.recommendation = text["recommendation"]
    db.session.add(roi)
    c.status = "report_ready"
    db.session.commit()
    return warnings


@app.post("/campaigns/<int:cid>/analyze")
@login_required("brand")
def analyze(cid):
    c = get_campaign(cid)
    if c.status not in ("reported", "report_ready") or c.report is None:
        flash("There are no results to analyse yet.", "error")
    elif any(p.paypal_payout_id for p in c.payments):
        flash("Payments were already released, so the analysis is locked.", "error")
    else:
        warnings = run_analysis(c)
        flash("Analysis complete.", "ok")
        for warning in warnings:
            flash(warning, "error")
    return back_to(c)


# --------------------------------------------------------------- payments
def release_payment(c, kind):
    """Pay the money Citra holds for this campaign out to the creator."""
    pay = c.payment(kind)
    if pay is None or pay.status != "completed":
        raise PayPalError("That payment has not been collected from the brand yet.")
    if pay.paypal_payout_id:
        return
    email = c.influencer.paypal_email
    if not email:
        raise PayPalError("The creator has no PayPal email on file.")
    batch_id, _status = paypal_client.send_payout(
        email, pay.amount, pay.currency, f"Citra {kind} payment: {c.title}",
        f"citra-{c.id}-{kind}-{pay.id}")
    pay.paypal_payout_id = batch_id
    pay.approved_by_brand = True
    if kind == "bonus":
        c.status = "bonus_paid"
    elif not (c.roi and c.roi.bonus_goal_met and c.bonus_amount > 0):
        c.status = "completed"
    db.session.commit()


def try_release(c, kind):
    try:
        release_payment(c, kind)
        flash("The payout was sent to the creator.", "ok")
    except PayPalError as exc:
        flash(f"The money was collected, but the payout failed: {exc}", "error")


@app.post("/campaigns/<int:cid>/pay/<kind>")
@login_required("brand")
def start_payment(cid, kind):
    c = get_campaign(cid)
    if kind == "base":
        allowed = c.status == "accepted"
        amount = c.base_payment
    elif kind == "bonus":
        base = c.payment("base")
        allowed = bool(c.status == "report_ready" and c.roi and c.roi.bonus_goal_met
                       and c.bonus_amount > 0 and base and base.paypal_payout_id)
        amount = c.bonus_amount
    else:
        abort(404)
    if not allowed:
        flash("This payment is not available right now.", "error")
        return back_to(c)
    pay = c.payment(kind)
    if pay and pay.status == "completed":
        flash("This payment was already collected.", "error")
        return back_to(c)
    try:
        order_id, approve_url = paypal_client.create_order(
            amount, c.currency, f"{c.title}: {kind} payment", f"campaign-{c.id}-{kind}",
            url_for("paypal_return", cid=c.id, kind=kind, _external=True),
            url_for("paypal_cancel", cid=c.id, kind=kind, _external=True))
    except PayPalError as exc:
        flash(str(exc), "error")
        return back_to(c)
    pay = pay or Payment(campaign_id=c.id, payment_type=kind, currency=c.currency)
    pay.amount = amount
    pay.paypal_order_id = order_id
    pay.status = "pending"
    db.session.add(pay)
    db.session.commit()
    return redirect(approve_url)


@app.route("/paypal/return/<int:cid>/<kind>")
@login_required("brand")
def paypal_return(cid, kind):
    c = get_campaign(cid)
    token = request.args.get("token", "")
    pay = c.payment(kind) if kind in ("base", "bonus") else None
    if pay is None or pay.paypal_order_id != token:
        flash("We could not match this PayPal payment to the campaign.", "error")
        return back_to(c)
    if pay.status != "completed":
        try:
            status, _capture_id = paypal_client.capture_order(token)
        except PayPalError as exc:
            flash(str(exc), "error")
            return back_to(c)
        if status != "COMPLETED":
            flash("PayPal did not complete the payment. No money was moved.", "error")
            return back_to(c)
        pay.status = "completed"
        if kind == "base":
            c.status = "paid_base"
        db.session.commit()
        flash("Payment received through PayPal. Citra holds it until the results are verified.", "ok")
    if kind == "bonus":
        try_release(c, "bonus")
    return back_to(c)


@app.route("/paypal/cancel/<int:cid>/<kind>")
@login_required("brand")
def paypal_cancel(cid, kind):
    flash("Payment cancelled. No money was moved.", "ok")
    return back_to(get_campaign(cid))


@app.post("/campaigns/<int:cid>/release/<kind>")
@login_required("brand")
def release(cid, kind):
    c = get_campaign(cid)
    if kind not in ("base", "bonus") or c.status != "report_ready" or c.roi is None:
        flash("Payments can be released once the report is ready.", "error")
    else:
        try_release(c, kind)
    return back_to(c)


# ------------------------------------------------------- brand interest feed
def creator_declined(brand_id, influencer_id):
    row = db.session.get(BrandInterest, (influencer_id, brand_id))
    return bool(row and row.status == "declined")


@app.route("/brands")
@login_required("influencer")
def brands_feed():
    show_passed = request.args.get("show") == "passed"
    mine = {r.brand_id: r.status for r in BrandInterest.query.filter_by(influencer_id=g.user.id)}
    brands = Brand.query.order_by(Brand.company_name).all()
    passed_count = sum(1 for b in brands if mine.get(b.user_id) == "declined")
    brands = [b for b in brands if (mine.get(b.user_id) == "declined") == show_passed]
    return render_template("brands.html", brands=brands, mine=mine,
                           show_passed=show_passed, passed_count=passed_count)


@app.post("/brands/<int:bid>/interest")
@login_required("influencer")
def set_interest(bid):
    brand = db.get_or_404(Brand, bid)
    action = request.form.get("action")
    row = db.session.get(BrandInterest, (g.user.id, brand.user_id))
    if action in ("interested", "declined"):
        if row:
            row.status = action
        else:
            db.session.add(BrandInterest(influencer_id=g.user.id, brand_id=brand.user_id, status=action))
        flash(f"We told {brand.company_name} you are interested." if action == "interested"
              else f"{brand.company_name} will not see you or send you offers.", "ok")
    elif action == "clear":
        if row:
            db.session.delete(row)
        flash(f"Your choice for {brand.company_name} was cleared.", "ok")
    else:
        abort(400)
    db.session.commit()
    return redirect(url_for("brands_feed", show=request.form.get("show") or None))


# ------------------------------------------------------------------ tasks
STEP_COUNT = 7
STATUS_STEP = {"offered": 1, "negotiating": 1, "accepted": 2, "paid_base": 3, "content_due": 4,
               "reported": 5, "report_ready": 6, "bonus_paid": 7, "completed": 7}


def user_campaigns():
    if g.user.role == "brand":
        query = Campaign.query.filter_by(brand_id=g.user.id)
    else:
        query = Campaign.query.filter_by(influencer_id=g.user.id)
    return query.order_by(Campaign.id.desc()).all()


def build_tasks(campaigns, role):
    """One entry per thing the user must do (kind 'action'), is waiting on others for
    ('waiting'), or has finished ('done'). Mirrors the panels on the campaign page."""
    today = date.today()
    items = []

    def add(c, text, kind, due=None):
        items.append({"c": c, "text": text, "kind": kind, "due": due,
                      "overdue": bool(due and kind == "action" and due < today),
                      "step": STATUS_STEP.get(c.status, 1)})

    for c in campaigns:
        if c.status == "rejected":
            continue
        if c.status in ("bonus_paid", "completed"):
            add(c, "Campaign complete", "done")
            continue
        base_pay, bonus_pay = c.payment("base"), c.payment("bonus")
        base_released = bool(base_pay and base_pay.paypal_payout_id)
        bonus_earned = bool(c.roi and c.roi.bonus_goal_met and c.bonus_amount > 0)
        brand, creator = c.brand.company_name, c.influencer.display_name
        if role == "influencer":
            if c.status == "offered":
                add(c, f"Answer the offer from {brand}", "action")
            elif c.status == "negotiating":
                add(c, f"Waiting for {brand} to answer your counter-proposal", "waiting")
            elif c.status in ("accepted", "paid_base") and not c.shipping:
                add(c, "Add your shipping details", "action")
            elif c.status == "accepted":
                add(c, f"Waiting for {brand} to fund the campaign", "waiting")
            elif c.status == "paid_base":
                add(c, f"Waiting for {brand} to send the product", "waiting")
            elif c.status == "content_due":
                add(c, "Post your content", "action", c.deadline_date)
                add(c, "Submit your results", "action", c.results_due_date)
            elif c.status == "reported":
                add(c, f"Waiting for {brand} to review your results", "waiting")
            elif c.status == "report_ready":
                if not base_released:
                    add(c, f"Waiting for {brand} to release your payment", "waiting")
                elif bonus_earned:
                    add(c, f"Waiting for {brand} to pay your bonus", "waiting")
        else:
            if c.status == "offered":
                add(c, f"Waiting for {creator} to answer your offer", "waiting")
            elif c.status == "negotiating":
                add(c, f"Answer {creator}'s counter-proposal", "action")
            elif c.status == "accepted":
                add(c, f"Fund the campaign: pay {money(c.base_payment)}", "action")
            elif c.status == "paid_base" and not c.shipping:
                add(c, f"Waiting for {creator}'s shipping details", "waiting")
            elif c.status == "paid_base":
                add(c, "Send the product and set the content deadline", "action")
            elif c.status == "content_due":
                add(c, f"Waiting for {creator} to post", "waiting", c.deadline_date)
            elif c.status == "reported":
                add(c, "Run the AI analysis of the results", "action")
            elif c.status == "report_ready":
                if not base_released:
                    add(c, f"Review the report and release {creator}'s payment", "action")
                elif bonus_earned and not (bonus_pay and bonus_pay.status == "completed"):
                    add(c, f"Pay the {money(c.bonus_amount)} performance bonus", "action")
                elif bonus_earned and bonus_pay and not bonus_pay.paypal_payout_id:
                    add(c, "Retry the bonus payout", "action")
    items.sort(key=lambda t: (t["due"] is None, t["due"] or date.max))
    return items


@app.route("/tasks")
@login_required()
def tasks():
    items = build_tasks(user_campaigns(), g.user.role)
    return render_template("tasks.html",
                           todo=[t for t in items if t["kind"] == "action"],
                           waiting=[t for t in items if t["kind"] == "waiting"],
                           done=[t for t in items if t["kind"] == "done"],
                           steps_total=STEP_COUNT)


@app.route("/uploads/<filename>")
@login_required()
def uploads(filename):
    report = MetricReport.query.filter_by(screenshot_path=filename).first_or_404()
    get_campaign(report.campaign_id)  # only campaign participants may view it
    return send_from_directory(UPLOAD_DIR, filename)


with app.app_context():
    db.create_all()  # creates only tables that do not exist yet (e.g. brand_interest)
    ensure_demo_passwords()

if __name__ == "__main__":
    app.run(debug=True)
