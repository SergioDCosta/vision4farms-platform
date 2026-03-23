# This is an auto-generated Django model module.
# You'll have to do the following manually to clean this up:
#   * Rearrange models' order
#   * Make sure each model has one field with primary_key=True
#   * Make sure each ForeignKey and OneToOneField has `on_delete` set to the desired behavior
#   * Remove `managed = False` lines if you wish to allow Django to create, modify, and delete the table
# Feel free to rename the models, but don't rename db_table values or field names.
from django.db import models


class AccountVerificationTokens(models.Model):
    id = models.UUIDField(primary_key=True)
    user = models.ForeignKey('Users', models.DO_NOTHING)
    token = models.CharField(unique=True, max_length=255)
    purpose = models.CharField(max_length=30)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'account_verification_tokens'


class AlertEvents(models.Model):
    id = models.UUIDField(primary_key=True)
    alert = models.ForeignKey('Alerts', models.DO_NOTHING)
    event_type = models.CharField(max_length=20)
    performed_by = models.ForeignKey('Users', models.DO_NOTHING, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'alert_events'


class Alerts(models.Model):
    id = models.UUIDField(primary_key=True)
    producer = models.ForeignKey('ProducerProfiles', models.DO_NOTHING)
    product = models.ForeignKey('Products', models.DO_NOTHING, blank=True, null=True)
    need = models.ForeignKey('Needs', models.DO_NOTHING, blank=True, null=True)
    forecast = models.ForeignKey('ProductionForecasts', models.DO_NOTHING, blank=True, null=True)
    listing = models.ForeignKey('MarketplaceListings', models.DO_NOTHING, blank=True, null=True)
    type = models.CharField(max_length=30)
    severity = models.CharField(max_length=20)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    source_system = models.CharField(max_length=30)
    status = models.CharField(max_length=20)
    assumed_loss = models.BooleanField()
    ignored_reason = models.TextField(blank=True, null=True)
    ignored_at = models.DateTimeField(blank=True, null=True)
    cleared_at = models.DateTimeField(blank=True, null=True)
    payload = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'alerts'


class AuditLog(models.Model):
    id = models.UUIDField(primary_key=True)
    user = models.ForeignKey('Users', models.DO_NOTHING, blank=True, null=True)
    action = models.CharField(max_length=100)
    entity_type = models.CharField(max_length=100, blank=True, null=True)
    entity_id = models.UUIDField(blank=True, null=True)
    old_values = models.JSONField(blank=True, null=True)
    new_values = models.JSONField(blank=True, null=True)
    ip_address = models.CharField(max_length=45, blank=True, null=True)
    user_agent = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'audit_log'


class ConversationParticipants(models.Model):
    id = models.UUIDField(primary_key=True)
    conversation = models.ForeignKey('Conversations', models.DO_NOTHING)
    user = models.ForeignKey('Users', models.DO_NOTHING)
    last_read_at = models.DateTimeField(blank=True, null=True)
    joined_at = models.DateTimeField()
    is_archived = models.BooleanField()

    class Meta:
        managed = False
        db_table = 'conversation_participants'
        unique_together = (('conversation', 'user'),)


class Conversations(models.Model):
    id = models.UUIDField(primary_key=True)
    conversation_type = models.CharField(max_length=20)
    title = models.CharField(max_length=255, blank=True, null=True)
    listing = models.ForeignKey('MarketplaceListings', models.DO_NOTHING, blank=True, null=True)
    order = models.ForeignKey('Orders', models.DO_NOTHING, blank=True, null=True)
    created_by = models.ForeignKey('Users', models.DO_NOTHING)
    is_active = models.BooleanField()
    last_message_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'conversations'


class MarketplaceListings(models.Model):
    id = models.UUIDField(primary_key=True)
    producer = models.ForeignKey('ProducerProfiles', models.DO_NOTHING)
    product = models.ForeignKey('Products', models.DO_NOTHING)
    stock = models.ForeignKey('Stocks', models.DO_NOTHING, blank=True, null=True)
    quantity_total = models.DecimalField(max_digits=14, decimal_places=3)
    quantity_available = models.DecimalField(max_digits=14, decimal_places=3)
    quantity_reserved = models.DecimalField(max_digits=14, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    delivery_mode = models.CharField(max_length=20)
    delivery_radius_km = models.DecimalField(max_digits=8, decimal_places=2, blank=True, null=True)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=20)
    published_at = models.DateTimeField()
    expires_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'marketplace_listings'


class Messages(models.Model):
    id = models.UUIDField(primary_key=True)
    conversation = models.ForeignKey(Conversations, models.DO_NOTHING)
    sender_user = models.ForeignKey('Users', models.DO_NOTHING, blank=True, null=True)
    message_type = models.CharField(max_length=20)
    content = models.TextField()
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'messages'


class Needs(models.Model):
    id = models.UUIDField(primary_key=True)
    producer = models.ForeignKey('ProducerProfiles', models.DO_NOTHING)
    product = models.ForeignKey('Products', models.DO_NOTHING)
    required_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    needed_by_date = models.DateTimeField(blank=True, null=True)
    source_system = models.CharField(max_length=30)
    external_id = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=30)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'needs'


class Notifications(models.Model):
    id = models.UUIDField(primary_key=True)
    user = models.ForeignKey('Users', models.DO_NOTHING)
    alert = models.ForeignKey(Alerts, models.DO_NOTHING, blank=True, null=True)
    order = models.ForeignKey('Orders', models.DO_NOTHING, blank=True, null=True)
    message = models.ForeignKey(Messages, models.DO_NOTHING, blank=True, null=True)
    recommendation = models.ForeignKey('Recommendations', models.DO_NOTHING, blank=True, null=True)
    type = models.CharField(max_length=30)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True, null=True)
    action_url = models.CharField(max_length=500, blank=True, null=True)
    is_read = models.BooleanField()
    read_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'notifications'


class OrderItems(models.Model):
    id = models.UUIDField(primary_key=True)
    order = models.ForeignKey('Orders', models.DO_NOTHING)
    listing = models.ForeignKey(MarketplaceListings, models.DO_NOTHING, blank=True, null=True)
    product = models.ForeignKey('Products', models.DO_NOTHING)
    seller_producer = models.ForeignKey('ProducerProfiles', models.DO_NOTHING)
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    item_status = models.CharField(max_length=20)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'order_items'


class OrderStatusHistory(models.Model):
    id = models.UUIDField(primary_key=True)
    order = models.ForeignKey('Orders', models.DO_NOTHING)
    status = models.CharField(max_length=20)
    changed_by = models.ForeignKey('Users', models.DO_NOTHING, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'order_status_history'


class Orders(models.Model):
    id = models.UUIDField(primary_key=True)
    order_number = models.BigAutoField(unique=True)
    buyer_producer = models.ForeignKey('ProducerProfiles', models.DO_NOTHING)
    source_type = models.CharField(max_length=20)
    recommendation = models.ForeignKey('Recommendations', models.DO_NOTHING, blank=True, null=True)
    status = models.CharField(max_length=20)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    delivery_method = models.CharField(max_length=20, blank=True, null=True)
    delivery_address = models.TextField(blank=True, null=True)
    delivery_city = models.CharField(max_length=255, blank=True, null=True)
    delivery_notes = models.TextField(blank=True, null=True)
    payment_method = models.CharField(max_length=50, blank=True, null=True)
    payment_status = models.CharField(max_length=20)
    buyer_notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()
    confirmed_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    cancelled_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'orders'


class ProducerProducts(models.Model):
    id = models.UUIDField(primary_key=True)
    producer = models.ForeignKey('ProducerProfiles', models.DO_NOTHING)
    product = models.ForeignKey('Products', models.DO_NOTHING)
    is_active = models.BooleanField()
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'producer_products'
        unique_together = (('producer', 'product'),)


class ProducerProfiles(models.Model):
    id = models.UUIDField(primary_key=True)
    user = models.OneToOneField('Users', models.DO_NOTHING)
    display_name = models.CharField(max_length=255)
    company_name = models.CharField(max_length=255, blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    nif = models.CharField(max_length=20, blank=True, null=True)
    address_line = models.CharField(max_length=255, blank=True, null=True)
    postal_code = models.CharField(max_length=20, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    district = models.CharField(max_length=100, blank=True, null=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    member_since = models.DateTimeField()
    rating_avg = models.DecimalField(max_digits=3, decimal_places=2, blank=True, null=True)
    completed_transactions_count = models.IntegerField()
    is_active_marketplace = models.BooleanField()
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'producer_profiles'


class ProductCategories(models.Model):
    id = models.UUIDField(primary_key=True)
    name = models.CharField(max_length=255)
    slug = models.CharField(unique=True, max_length=255)
    is_active = models.BooleanField()
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'product_categories'


class ProductionForecasts(models.Model):
    id = models.UUIDField(primary_key=True)
    producer = models.ForeignKey(ProducerProfiles, models.DO_NOTHING)
    product = models.ForeignKey('Products', models.DO_NOTHING)
    forecast_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    period_start = models.DateTimeField(blank=True, null=True)
    period_end = models.DateTimeField(blank=True, null=True)
    confidence_score = models.DecimalField(max_digits=4, decimal_places=3, blank=True, null=True)
    source_system = models.CharField(max_length=30)
    external_id = models.CharField(max_length=100, blank=True, null=True)
    source_payload = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'production_forecasts'


class Products(models.Model):
    id = models.UUIDField(primary_key=True)
    category = models.ForeignKey(ProductCategories, models.DO_NOTHING, blank=True, null=True)
    name = models.CharField(max_length=255)
    slug = models.CharField(unique=True, max_length=255)
    unit = models.CharField(max_length=50)
    description = models.TextField(blank=True, null=True)
    is_active = models.BooleanField()
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'products'


class RecommendationItems(models.Model):
    id = models.UUIDField(primary_key=True)
    recommendation = models.ForeignKey('Recommendations', models.DO_NOTHING)
    listing = models.ForeignKey(MarketplaceListings, models.DO_NOTHING)
    seller_producer = models.ForeignKey(ProducerProfiles, models.DO_NOTHING)
    product = models.ForeignKey(Products, models.DO_NOTHING)
    suggested_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    position = models.IntegerField()
    is_selected = models.BooleanField()
    reasons = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'recommendation_items'


class Recommendations(models.Model):
    id = models.UUIDField(primary_key=True)
    producer = models.ForeignKey(ProducerProfiles, models.DO_NOTHING)
    product = models.ForeignKey(Products, models.DO_NOTHING)
    generated_from_alert = models.ForeignKey(Alerts, models.DO_NOTHING, blank=True, null=True)
    requested_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    deadline_date = models.DateTimeField(blank=True, null=True)
    deficit_quantity = models.DecimalField(max_digits=14, decimal_places=3, blank=True, null=True)
    source_type = models.CharField(max_length=30)
    status = models.CharField(max_length=20)
    summary_text = models.TextField(blank=True, null=True)
    reason_summary = models.TextField(blank=True, null=True)
    estimated_total = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()
    accepted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'recommendations'


class StockMovements(models.Model):
    id = models.UUIDField(primary_key=True)
    stock = models.ForeignKey('Stocks', models.DO_NOTHING)
    movement_type = models.CharField(max_length=50)
    quantity_delta = models.DecimalField(max_digits=14, decimal_places=3)
    reference_type = models.CharField(max_length=50, blank=True, null=True)
    reference_id = models.UUIDField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    performed_by = models.ForeignKey('Users', models.DO_NOTHING, blank=True, null=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'stock_movements'


class Stocks(models.Model):
    id = models.UUIDField(primary_key=True)
    producer = models.ForeignKey(ProducerProfiles, models.DO_NOTHING)
    product = models.ForeignKey(Products, models.DO_NOTHING)
    current_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    reserved_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    minimum_threshold = models.DecimalField(max_digits=14, decimal_places=3)
    updated_by = models.ForeignKey('Users', models.DO_NOTHING, blank=True, null=True)
    last_updated_at = models.DateTimeField()
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'stocks'
        unique_together = (('producer', 'product'),)


class Users(models.Model):
    id = models.UUIDField(primary_key=True)
    email = models.CharField(unique=True, max_length=255)
    password = models.CharField(max_length=128)
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    role = models.CharField(max_length=20)
    registration_source = models.CharField(max_length=30)
    account_status = models.CharField(max_length=40)
    email_verified_at = models.DateTimeField(blank=True, null=True)
    is_active = models.BooleanField()
    is_staff = models.BooleanField()
    last_login = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'users'


class Vision4FarmsSyncLog(models.Model):
    id = models.UUIDField(primary_key=True)
    sync_type = models.CharField(max_length=30)
    status = models.CharField(max_length=20)
    records_received = models.IntegerField()
    records_imported = models.IntegerField()
    records_skipped = models.IntegerField()
    error_message = models.TextField(blank=True, null=True)
    payload_summary = models.JSONField(blank=True, null=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'vision4farms_sync_log'
