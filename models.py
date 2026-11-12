from datetime import timedelta

from flask_sqlalchemy import SQLAlchemy

RESULTS_WINDOW_DAYS = 30  # results are due a month after the content deadline

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'brand' | 'influencer'
    brand = db.relationship("Brand", uselist=False, back_populates="user")
    influencer = db.relationship("Influencer", uselist=False, back_populates="user")


class Brand(db.Model):
    __tablename__ = "brands"
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    company_name = db.Column(db.String(150), nullable=False)
    industry = db.Column(db.String(100))
    description = db.Column(db.Text)
    user = db.relationship("User", back_populates="brand")


class Influencer(db.Model):
    __tablename__ = "influencers"
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    display_name = db.Column(db.String(150), nullable=False)
    niche = db.Column(db.String(100))
    bio = db.Column(db.Text)
    values_text = db.Column(db.Text)
    followers = db.Column(db.Integer, default=0)
    avg_monthly_views = db.Column(db.Integer, default=0)
    paypal_email = db.Column(db.String(255))
    user = db.relationship("User", back_populates="influencer")


class Campaign(db.Model):
    __tablename__ = "campaigns"
    id = db.Column(db.Integer, primary_key=True)
    brand_id = db.Column(db.Integer, db.ForeignKey("brands.user_id"), nullable=False)
    influencer_id = db.Column(db.Integer, db.ForeignKey("influencers.user_id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    product_name = db.Column(db.String(200), nullable=False)
    product_category = db.Column(db.String(100))
    description = db.Column(db.Text)
    videos_count = db.Column(db.Integer, nullable=False, default=1)
    base_payment = db.Column(db.Numeric(10, 2), nullable=False)
    currency = db.Column(db.String(3), nullable=False, default="USD")
    bonus_metric = db.Column(db.String(20), nullable=False, default="views")
    bonus_threshold = db.Column(db.Integer, nullable=False, default=0)
    bonus_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    start_date = db.Column(db.Date)      # set when the brand marks the product as delivered
    deadline_date = db.Column(db.Date)
    status = db.Column(db.String(20), nullable=False, default="offered")
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    brand = db.relationship("Brand")
    influencer = db.relationship("Influencer")
    negotiations = db.relationship("Negotiation", order_by="Negotiation.id")
    shipping = db.relationship("ShippingInfo", uselist=False)
    report = db.relationship("MetricReport", uselist=False)
    roi = db.relationship("RoiReport", uselist=False)
    payments = db.relationship("Payment", order_by="Payment.id")

    def payment(self, kind):
        """The 'base' or 'bonus' payment record of this campaign, if any."""
        return next((p for p in self.payments if p.payment_type == kind), None)

    @property
    def results_due_date(self):
        if not self.deadline_date:
            return None
        return self.deadline_date + timedelta(days=RESULTS_WINDOW_DAYS)


class Negotiation(db.Model):
    __tablename__ = "negotiations"
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id"), nullable=False)
    proposed_by = db.Column(db.String(20), nullable=False)
    videos_count = db.Column(db.Integer)
    message = db.Column(db.Text)


class ShippingInfo(db.Model):
    __tablename__ = "shipping_info"
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id"), nullable=False, unique=True)
    full_name = db.Column(db.String(150), nullable=False)
    address_line = db.Column(db.String(255), nullable=False)
    city = db.Column(db.String(100), nullable=False)
    postal_code = db.Column(db.String(20))
    country = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(30))
    clothing_size = db.Column(db.String(10))


class MetricReport(db.Model):
    __tablename__ = "metric_reports"
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id"), nullable=False, unique=True)
    reported_views = db.Column(db.Integer, nullable=False)
    reported_likes = db.Column(db.Integer, nullable=False)
    reported_comments = db.Column(db.Integer, nullable=False)
    screenshot_path = db.Column(db.String(255))
    ai_views = db.Column(db.Integer)
    ai_likes = db.Column(db.Integer)
    ai_comments = db.Column(db.Integer)
    metrics_match = db.Column(db.Boolean)
    verification_note = db.Column(db.Text)


class RoiReport(db.Model):
    __tablename__ = "roi_reports"
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id"), nullable=False, unique=True)
    total_spent = db.Column(db.Numeric(10, 2), nullable=False)
    cost_per_view = db.Column(db.Numeric(10, 4))
    cost_per_engagement = db.Column(db.Numeric(10, 4))
    engagement_rate = db.Column(db.Numeric(6, 3))
    bonus_goal_met = db.Column(db.Boolean, nullable=False, default=False)
    summary_text = db.Column(db.Text)
    recommendation = db.Column(db.Text)


class Payment(db.Model):
    """One row per campaign and kind ('base' or 'bonus').
    status 'completed' means the brand's money was collected by Citra (held in escrow);
    paypal_payout_id is filled once that money was paid out to the creator."""
    __tablename__ = "payments"
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id"), nullable=False)
    payment_type = db.Column(db.String(10), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    currency = db.Column(db.String(3), nullable=False, default="USD")
    paypal_order_id = db.Column(db.String(100))
    paypal_payout_id = db.Column(db.String(100))
    status = db.Column(db.String(20), nullable=False, default="pending")
    approved_by_brand = db.Column(db.Boolean, nullable=False, default=False)


class BrandInterest(db.Model):
    """A creator tells a brand 'interested' or 'declined'. Declined hides the creator from that brand."""
    __tablename__ = "brand_interest"
    influencer_id = db.Column(db.Integer, db.ForeignKey("influencers.user_id"), primary_key=True)
    brand_id = db.Column(db.Integer, db.ForeignKey("brands.user_id"), primary_key=True)
    status = db.Column(db.String(12), nullable=False)  # 'interested' | 'declined'
    created_at = db.Column(db.DateTime, server_default=db.func.now())
