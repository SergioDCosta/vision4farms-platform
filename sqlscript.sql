--
-- PostgreSQL database dump
--

\restrict pdTPoMqzpYQWMth4tieBkfo280mXoCPDlAWZgzGx8ORHNjN511hk8YT4XsgJbeA

-- Dumped from database version 18.3 (Debian 18.3-1.pgdg13+1)
-- Dumped by pg_dump version 18.4

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;


--
-- Name: EXTENSION pgcrypto; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';


--
-- Name: uuid-ossp; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public;


--
-- Name: EXTENSION "uuid-ossp"; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION "uuid-ossp" IS 'generate universally unique identifiers (UUIDs)';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: account_verification_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_verification_tokens (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    user_id uuid NOT NULL,
    token character varying(255) NOT NULL,
    purpose character varying(30) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT account_verification_tokens_purpose_check CHECK (((purpose)::text = ANY (ARRAY[('SIGNUP_CONFIRMATION'::character varying)::text, ('ADMIN_INVITE'::character varying)::text, ('PASSWORD_RESET'::character varying)::text])))
);


--
-- Name: alert_deliveries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alert_deliveries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    alert_id uuid NOT NULL,
    user_id uuid NOT NULL,
    channel character varying(20) NOT NULL,
    status character varying(20) DEFAULT 'PENDING'::character varying NOT NULL,
    error text,
    sent_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT alert_deliveries_channel_check CHECK (((channel)::text = ANY ((ARRAY['IN_APP'::character varying, 'EMAIL'::character varying, 'SMS'::character varying])::text[]))),
    CONSTRAINT alert_deliveries_status_check CHECK (((status)::text = ANY ((ARRAY['PENDING'::character varying, 'SENT'::character varying, 'SKIPPED'::character varying, 'FAILED'::character varying])::text[])))
);


--
-- Name: alert_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alert_events (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    alert_id uuid NOT NULL,
    event_type character varying(20) NOT NULL,
    performed_by_id uuid,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT alert_events_event_type_check CHECK (((event_type)::text = ANY (ARRAY[('CREATED'::character varying)::text, ('READ'::character varying)::text, ('RESOLVED'::character varying)::text, ('IGNORED'::character varying)::text, ('CLEARED'::character varying)::text])))
);


--
-- Name: alerts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alerts (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    producer_id uuid NOT NULL,
    product_id uuid,
    need_id uuid,
    forecast_id uuid,
    listing_id uuid,
    type character varying(30) NOT NULL,
    severity character varying(20) NOT NULL,
    title character varying(255) NOT NULL,
    description text,
    source_system character varying(30) NOT NULL,
    status character varying(20) DEFAULT 'ACTIVE'::character varying NOT NULL,
    assumed_loss boolean DEFAULT false NOT NULL,
    ignored_reason text,
    ignored_at timestamp with time zone,
    cleared_at timestamp with time zone,
    payload jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    category character varying(30) DEFAULT 'SYSTEM'::character varying NOT NULL,
    context_key character varying(180),
    requires_action boolean DEFAULT false NOT NULL,
    due_at timestamp with time zone,
    read_at timestamp with time zone,
    snoozed_until timestamp with time zone,
    expires_at timestamp with time zone,
    priority smallint DEFAULT 50 NOT NULL,
    CONSTRAINT alerts_category_check CHECK (((category)::text = ANY ((ARRAY['STOCK'::character varying, 'NEEDS'::character varying, 'ORDERS'::character varying, 'MARKETPLACE'::character varying, 'MESSAGES'::character varying, 'SYSTEM'::character varying])::text[]))),
    CONSTRAINT alerts_severity_check CHECK (((severity)::text = ANY (ARRAY[('INFO'::character varying)::text, ('WARNING'::character varying)::text, ('CRITICAL'::character varying)::text]))),
    CONSTRAINT alerts_source_system_check CHECK (((source_system)::text = ANY (ARRAY[('INTERNAL'::character varying)::text, ('VISION4FARMS'::character varying)::text, ('MANUAL'::character varying)::text]))),
    CONSTRAINT alerts_status_check CHECK (((status)::text = ANY (ARRAY[('ACTIVE'::character varying)::text, ('READ'::character varying)::text, ('RESOLVED'::character varying)::text, ('IGNORED'::character varying)::text, ('CLEARED'::character varying)::text]))),
    CONSTRAINT alerts_type_check CHECK (((type)::text = ANY ((ARRAY['SHORTAGE'::character varying, 'CRITICAL_STOCK'::character varying, 'SURPLUS_AVAILABLE'::character varying, 'BUY_OPPORTUNITY'::character varying, 'SELL_SUGGESTION'::character varying, 'EXTERNAL_DEFICIT'::character varying, 'NEED_UNDERCOVERED'::character varying, 'NEED_RESPONSE_RECEIVED'::character varying, 'NEED_DEADLINE_APPROACHING'::character varying, 'OFFER_REJECTED'::character varying, 'ORDER_REQUIRES_CONFIRMATION'::character varying, 'ORDER_DELIVERY_OVERDUE'::character varying, 'ORDER_PURCHASE_CREATED'::character varying, 'ORDER_CONFIRMED'::character varying, 'ORDER_IN_PROGRESS'::character varying, 'ORDER_DELIVERING'::character varying, 'ORDER_CANCELLED'::character varying, 'ORDER_COMPLETED'::character varying, 'LISTING_EXPIRING_SOON'::character varying, 'MESSAGE_UNREAD'::character varying])::text[])))
);


--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_log (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    user_id uuid,
    action character varying(100) NOT NULL,
    entity_type character varying(100),
    entity_id uuid,
    old_values jsonb,
    new_values jsonb,
    ip_address character varying(45),
    user_agent text,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: auth_group; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_group (
    id integer NOT NULL,
    name character varying(150) NOT NULL
);


--
-- Name: auth_group_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.auth_group ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.auth_group_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: auth_group_permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_group_permissions (
    id bigint NOT NULL,
    group_id integer NOT NULL,
    permission_id integer NOT NULL
);


--
-- Name: auth_group_permissions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.auth_group_permissions ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.auth_group_permissions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: auth_permission; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_permission (
    id integer NOT NULL,
    name character varying(255) NOT NULL,
    content_type_id integer NOT NULL,
    codename character varying(100) NOT NULL
);


--
-- Name: auth_permission_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.auth_permission ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.auth_permission_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: auth_user; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_user (
    id integer NOT NULL,
    password character varying(128) NOT NULL,
    last_login timestamp with time zone,
    is_superuser boolean NOT NULL,
    username character varying(150) NOT NULL,
    first_name character varying(150) NOT NULL,
    last_name character varying(150) NOT NULL,
    email character varying(254) NOT NULL,
    is_staff boolean NOT NULL,
    is_active boolean NOT NULL,
    date_joined timestamp with time zone NOT NULL
);


--
-- Name: auth_user_groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_user_groups (
    id bigint NOT NULL,
    user_id integer NOT NULL,
    group_id integer NOT NULL
);


--
-- Name: auth_user_groups_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.auth_user_groups ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.auth_user_groups_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: auth_user_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.auth_user ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.auth_user_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: auth_user_user_permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_user_user_permissions (
    id bigint NOT NULL,
    user_id integer NOT NULL,
    permission_id integer NOT NULL
);


--
-- Name: auth_user_user_permissions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.auth_user_user_permissions ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.auth_user_user_permissions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: conversation_participants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.conversation_participants (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    conversation_id uuid NOT NULL,
    user_id uuid NOT NULL,
    last_read_at timestamp with time zone,
    joined_at timestamp with time zone DEFAULT now() NOT NULL,
    is_archived boolean DEFAULT false NOT NULL
);


--
-- Name: conversations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.conversations (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    conversation_type character varying(20) NOT NULL,
    title character varying(255),
    listing_id uuid,
    order_id uuid,
    created_by_id uuid NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    last_message_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT conversations_conversation_type_check CHECK (((conversation_type)::text = ANY (ARRAY[('DIRECT'::character varying)::text, ('LISTING_CONTACT'::character varying)::text, ('ORDER_CONTACT'::character varying)::text])))
);


--
-- Name: django_admin_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.django_admin_log (
    id integer NOT NULL,
    action_time timestamp with time zone NOT NULL,
    object_id text,
    object_repr character varying(200) NOT NULL,
    action_flag smallint NOT NULL,
    change_message text NOT NULL,
    content_type_id integer,
    user_id integer NOT NULL,
    CONSTRAINT django_admin_log_action_flag_check CHECK ((action_flag >= 0))
);


--
-- Name: django_admin_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.django_admin_log ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.django_admin_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: django_content_type; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.django_content_type (
    id integer NOT NULL,
    app_label character varying(100) NOT NULL,
    model character varying(100) NOT NULL
);


--
-- Name: django_content_type_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.django_content_type ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.django_content_type_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: django_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.django_migrations (
    id bigint NOT NULL,
    app character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    applied timestamp with time zone NOT NULL
);


--
-- Name: django_migrations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.django_migrations ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.django_migrations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: django_session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.django_session (
    session_key character varying(40) NOT NULL,
    session_data text NOT NULL,
    expire_date timestamp with time zone NOT NULL
);


--
-- Name: external_customer_demands; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.external_customer_demands (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    producer_id uuid NOT NULL,
    product_id uuid NOT NULL,
    client_name character varying(255) NOT NULL,
    client_contact character varying(255),
    client_reference character varying(120),
    requested_quantity numeric(14,3) NOT NULL,
    requested_delivery_date date NOT NULL,
    status character varying(30) DEFAULT 'OPEN'::character varying NOT NULL,
    notes text,
    generated_need_id uuid,
    source_system character varying(30) DEFAULT 'MANUAL'::character varying NOT NULL,
    external_id character varying(120),
    created_by_id uuid,
    updated_by_id uuid,
    cancelled_at timestamp with time zone,
    fulfilled_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT external_customer_demands_client_name_not_blank_check CHECK ((length(TRIM(BOTH FROM client_name)) > 0)),
    CONSTRAINT external_customer_demands_requested_quantity_check CHECK ((requested_quantity > (0)::numeric)),
    CONSTRAINT external_customer_demands_source_system_check CHECK (((source_system)::text = ANY ((ARRAY['MANUAL'::character varying, 'VISION4FARMS'::character varying, 'IMPORT'::character varying, 'API'::character varying])::text[]))),
    CONSTRAINT external_customer_demands_status_check CHECK (((status)::text = ANY ((ARRAY['OPEN'::character varying, 'PARTIALLY_COVERED'::character varying, 'COVERED'::character varying, 'FULFILLED'::character varying, 'CANCELLED'::character varying])::text[])))
);


--
-- Name: marketplace_listings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.marketplace_listings (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    producer_id uuid NOT NULL,
    product_id uuid NOT NULL,
    stock_id uuid,
    quantity_total numeric(14,3) NOT NULL,
    quantity_available numeric(14,3) NOT NULL,
    quantity_reserved numeric(14,3) DEFAULT 0 NOT NULL,
    unit_price numeric(12,2) NOT NULL,
    delivery_mode character varying(20) NOT NULL,
    delivery_radius_km numeric(8,2),
    delivery_fee numeric(10,2),
    notes text,
    status character varying(20) DEFAULT 'ACTIVE'::character varying NOT NULL,
    published_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    photo_path character varying(255),
    forecast_id uuid,
    need_id uuid,
    show_location_on_map boolean DEFAULT true NOT NULL,
    need_response_status character varying(20) DEFAULT 'PENDING'::character varying NOT NULL,
    CONSTRAINT listing_qty_consistency CHECK (((quantity_available + quantity_reserved) <= quantity_total)),
    CONSTRAINT marketplace_listings_delivery_fee_check CHECK (((delivery_fee IS NULL) OR (delivery_fee >= (0)::numeric))),
    CONSTRAINT marketplace_listings_delivery_mode_check CHECK (((delivery_mode)::text = ANY (ARRAY[('PICKUP'::character varying)::text, ('DELIVERY'::character varying)::text, ('BOTH'::character varying)::text]))),
    CONSTRAINT marketplace_listings_delivery_radius_km_check CHECK (((delivery_radius_km IS NULL) OR (delivery_radius_km >= (0)::numeric))),
    CONSTRAINT marketplace_listings_need_response_status_check CHECK (((need_response_status)::text = ANY ((ARRAY['PENDING'::character varying, 'ACCEPTED'::character varying, 'REJECTED'::character varying, 'CANCELLED'::character varying, 'COMPLETED'::character varying, 'WITHDRAWN'::character varying, 'EXPIRED'::character varying])::text[]))),
    CONSTRAINT marketplace_listings_quantity_available_check CHECK ((quantity_available >= (0)::numeric)),
    CONSTRAINT marketplace_listings_quantity_reserved_check CHECK ((quantity_reserved >= (0)::numeric)),
    CONSTRAINT marketplace_listings_quantity_total_check CHECK ((quantity_total > (0)::numeric)),
    CONSTRAINT marketplace_listings_source_xor_chk CHECK ((((stock_id IS NOT NULL) AND (forecast_id IS NULL)) OR ((stock_id IS NULL) AND (forecast_id IS NOT NULL)))),
    CONSTRAINT marketplace_listings_status_check CHECK (((status)::text = ANY (ARRAY[('ACTIVE'::character varying)::text, ('RESERVED'::character varying)::text, ('CLOSED'::character varying)::text, ('EXPIRED'::character varying)::text, ('CANCELLED'::character varying)::text]))),
    CONSTRAINT marketplace_listings_unit_price_check CHECK ((unit_price > (0)::numeric))
);


--
-- Name: messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.messages (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    conversation_id uuid NOT NULL,
    sender_user_id uuid,
    message_type character varying(20) DEFAULT 'TEXT'::character varying NOT NULL,
    content text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    attachment_url text,
    attachment_name character varying(255),
    attachment_type character varying(50),
    CONSTRAINT messages_message_type_check CHECK (((message_type)::text = ANY (ARRAY['TEXT'::text, 'SYSTEM_EVENT'::text, 'FILE'::text])))
);


--
-- Name: needs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.needs (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    producer_id uuid NOT NULL,
    product_id uuid NOT NULL,
    required_quantity numeric(14,3) NOT NULL,
    needed_by_date timestamp with time zone,
    source_system character varying(30) DEFAULT 'MANUAL'::character varying NOT NULL,
    external_id character varying(100),
    status character varying(30) DEFAULT 'OPEN'::character varying NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT needs_required_quantity_check CHECK ((required_quantity > (0)::numeric)),
    CONSTRAINT needs_source_system_check CHECK (((source_system)::text = ANY ((ARRAY['MANUAL'::character varying, 'VISION4FARMS'::character varying, 'ALERT'::character varying, 'CUSTOMER_DEMAND'::character varying])::text[]))),
    CONSTRAINT needs_status_check CHECK (((status)::text = ANY (ARRAY[('OPEN'::character varying)::text, ('PARTIALLY_COVERED'::character varying)::text, ('COVERED'::character varying)::text, ('IGNORED'::character varying)::text, ('CANCELLED'::character varying)::text])))
);


--
-- Name: notifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notifications (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    user_id uuid NOT NULL,
    alert_id uuid,
    order_id uuid,
    message_id uuid,
    recommendation_id uuid,
    type character varying(30) NOT NULL,
    title character varying(255) NOT NULL,
    body text,
    action_url character varying(500),
    is_read boolean DEFAULT false NOT NULL,
    read_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT notifications_type_check CHECK (((type)::text = ANY (ARRAY[('ALERT'::character varying)::text, ('MESSAGE'::character varying)::text, ('ORDER_UPDATE'::character varying)::text, ('RECOMMENDATION'::character varying)::text, ('SYSTEM'::character varying)::text, ('ACCOUNT'::character varying)::text])))
);


--
-- Name: order_groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.order_groups (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    group_number bigint NOT NULL,
    buyer_producer_id uuid NOT NULL,
    source_type character varying(30),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: order_groups_group_number_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.order_groups ALTER COLUMN group_number ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.order_groups_group_number_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: order_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.order_items (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    order_id uuid NOT NULL,
    listing_id uuid,
    product_id uuid NOT NULL,
    seller_producer_id uuid NOT NULL,
    quantity numeric(14,3) NOT NULL,
    unit_price numeric(12,2) NOT NULL,
    subtotal numeric(12,2) NOT NULL,
    item_status character varying(20) DEFAULT 'PENDING'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    need_id uuid,
    CONSTRAINT order_items_item_status_check CHECK (((item_status)::text = ANY (ARRAY[('PENDING'::character varying)::text, ('CONFIRMED'::character varying)::text, ('IN_DELIVERY'::character varying)::text, ('COMPLETED'::character varying)::text, ('CANCELLED'::character varying)::text]))),
    CONSTRAINT order_items_quantity_check CHECK ((quantity > (0)::numeric)),
    CONSTRAINT order_items_subtotal_check CHECK ((subtotal >= (0)::numeric)),
    CONSTRAINT order_items_unit_price_check CHECK ((unit_price > (0)::numeric))
);


--
-- Name: order_status_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.order_status_history (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    order_id uuid NOT NULL,
    status character varying(20) NOT NULL,
    changed_by_id uuid,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT order_status_history_status_check CHECK (((status)::text = ANY (ARRAY[('PENDING'::character varying)::text, ('CONFIRMED'::character varying)::text, ('IN_PROGRESS'::character varying)::text, ('DELIVERING'::character varying)::text, ('COMPLETED'::character varying)::text, ('CANCELLED'::character varying)::text])))
);


--
-- Name: orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.orders (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    order_number bigint NOT NULL,
    buyer_producer_id uuid NOT NULL,
    source_type character varying(20) DEFAULT 'MARKETPLACE'::character varying NOT NULL,
    recommendation_id uuid,
    status character varying(20) DEFAULT 'PENDING'::character varying NOT NULL,
    total_amount numeric(12,2) DEFAULT 0 NOT NULL,
    delivery_method character varying(20),
    delivery_address text,
    delivery_city character varying(255),
    delivery_notes text,
    payment_method character varying(50),
    payment_status character varying(20) DEFAULT 'PENDING'::character varying NOT NULL,
    buyer_notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    confirmed_at timestamp with time zone,
    completed_at timestamp with time zone,
    cancelled_at timestamp with time zone,
    group_id uuid,
    CONSTRAINT orders_delivery_method_check CHECK (((delivery_method)::text = ANY (ARRAY[('PICKUP'::character varying)::text, ('DELIVERY'::character varying)::text, ('MIXED'::character varying)::text]))),
    CONSTRAINT orders_payment_status_check CHECK (((payment_status)::text = ANY (ARRAY[('PENDING'::character varying)::text, ('PAID'::character varying)::text, ('FAILED'::character varying)::text]))),
    CONSTRAINT orders_source_type_check CHECK (((source_type)::text = ANY (ARRAY[('MARKETPLACE'::character varying)::text, ('RECOMMENDATION'::character varying)::text]))),
    CONSTRAINT orders_status_check CHECK (((status)::text = ANY (ARRAY[('PENDING'::character varying)::text, ('CONFIRMED'::character varying)::text, ('IN_PROGRESS'::character varying)::text, ('DELIVERING'::character varying)::text, ('COMPLETED'::character varying)::text, ('CANCELLED'::character varying)::text]))),
    CONSTRAINT orders_total_amount_check CHECK ((total_amount >= (0)::numeric))
);


--
-- Name: orders_order_number_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.orders ALTER COLUMN order_number ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.orders_order_number_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: producer_products; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.producer_products (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    producer_id uuid NOT NULL,
    product_id uuid NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    producer_description text
);


--
-- Name: producer_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.producer_profiles (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    user_id uuid NOT NULL,
    display_name character varying(255) NOT NULL,
    company_name character varying(255),
    phone character varying(20),
    nif character varying(20),
    address_line character varying(255),
    postal_code character varying(20),
    city character varying(100),
    district character varying(100),
    latitude numeric(9,6),
    longitude numeric(9,6),
    member_since timestamp with time zone DEFAULT now() NOT NULL,
    rating_avg numeric(3,2),
    completed_transactions_count integer DEFAULT 0 NOT NULL,
    is_active_marketplace boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    user_type character varying(30),
    CONSTRAINT producer_profiles_completed_transactions_count_check CHECK ((completed_transactions_count >= 0)),
    CONSTRAINT producer_profiles_user_type_check CHECK (((user_type)::text = ANY (ARRAY[('AGRICULTOR'::character varying)::text, ('DISTRIBUIDOR'::character varying)::text, ('VENDEDOR'::character varying)::text])))
);


--
-- Name: product_categories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product_categories (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    name character varying(255) NOT NULL,
    slug character varying(255) NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: production_forecasts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.production_forecasts (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    producer_id uuid NOT NULL,
    product_id uuid NOT NULL,
    forecast_quantity numeric(14,3) NOT NULL,
    period_start timestamp with time zone,
    period_end timestamp with time zone,
    confidence_score numeric(4,3),
    source_system character varying(30) DEFAULT 'MANUAL'::character varying NOT NULL,
    external_id character varying(100),
    source_payload jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    reserved_quantity numeric(14,3) DEFAULT 0 NOT NULL,
    is_marketplace_enabled boolean DEFAULT false NOT NULL,
    notes text,
    CONSTRAINT production_forecasts_confidence_score_check CHECK (((confidence_score IS NULL) OR ((confidence_score >= (0)::numeric) AND (confidence_score <= (1)::numeric)))),
    CONSTRAINT production_forecasts_forecast_quantity_check CHECK ((forecast_quantity >= (0)::numeric)),
    CONSTRAINT production_forecasts_source_system_check CHECK (((source_system)::text = ANY (ARRAY[('MANUAL'::character varying)::text, ('VISION4FARMS'::character varying)::text, ('MODEL'::character varying)::text])))
);


--
-- Name: products; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.products (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    category_id uuid,
    name character varying(255) NOT NULL,
    slug character varying(255) NOT NULL,
    unit character varying(50) NOT NULL,
    description text,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: recommendation_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.recommendation_items (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    recommendation_id uuid NOT NULL,
    listing_id uuid NOT NULL,
    seller_producer_id uuid NOT NULL,
    product_id uuid NOT NULL,
    suggested_quantity numeric(14,3) NOT NULL,
    unit_price numeric(12,2) NOT NULL,
    subtotal numeric(12,2) NOT NULL,
    "position" integer DEFAULT 1 NOT NULL,
    is_selected boolean DEFAULT true NOT NULL,
    reasons jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT recommendation_items_position_check CHECK (("position" > 0)),
    CONSTRAINT recommendation_items_subtotal_check CHECK ((subtotal >= (0)::numeric)),
    CONSTRAINT recommendation_items_suggested_quantity_check CHECK ((suggested_quantity > (0)::numeric)),
    CONSTRAINT recommendation_items_unit_price_check CHECK ((unit_price > (0)::numeric))
);


--
-- Name: recommendations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.recommendations (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    producer_id uuid NOT NULL,
    product_id uuid NOT NULL,
    generated_from_alert_id uuid,
    requested_quantity numeric(14,3) NOT NULL,
    deadline_date timestamp with time zone,
    deficit_quantity numeric(14,3),
    source_type character varying(30) DEFAULT 'MANUAL'::character varying NOT NULL,
    status character varying(20) DEFAULT 'GENERATED'::character varying NOT NULL,
    summary_text text,
    reason_summary text,
    estimated_total numeric(12,2),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    accepted_at timestamp with time zone,
    need_id uuid,
    CONSTRAINT recommendations_deficit_quantity_check CHECK (((deficit_quantity IS NULL) OR (deficit_quantity >= (0)::numeric))),
    CONSTRAINT recommendations_estimated_total_check CHECK (((estimated_total IS NULL) OR (estimated_total >= (0)::numeric))),
    CONSTRAINT recommendations_requested_quantity_check CHECK ((requested_quantity > (0)::numeric)),
    CONSTRAINT recommendations_source_type_check CHECK (((source_type)::text = ANY (ARRAY[('MANUAL'::character varying)::text, ('ALERT'::character varying)::text, ('VISION4FARMS'::character varying)::text]))),
    CONSTRAINT recommendations_status_check CHECK (((status)::text = ANY (ARRAY[('GENERATED'::character varying)::text, ('ACCEPTED'::character varying)::text, ('ADJUSTED'::character varying)::text, ('IGNORED'::character varying)::text, ('EXPIRED'::character varying)::text])))
);


--
-- Name: stock_movements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stock_movements (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    stock_id uuid NOT NULL,
    movement_type character varying(50) NOT NULL,
    quantity_delta numeric(14,3) NOT NULL,
    reference_type character varying(50),
    reference_id uuid,
    notes text,
    performed_by_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT stock_movements_movement_type_check CHECK (((movement_type)::text = ANY (ARRAY[('MANUAL_ADJUSTMENT'::character varying)::text, ('ORDER_IN'::character varying)::text, ('ORDER_OUT'::character varying)::text, ('IMPORT'::character varying)::text, ('CORRECTION'::character varying)::text, ('LISTING_PUBLISH'::character varying)::text, ('LISTING_CANCEL'::character varying)::text]))),
    CONSTRAINT stock_movements_quantity_delta_check CHECK ((quantity_delta <> (0)::numeric))
);


--
-- Name: stocks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stocks (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    producer_id uuid NOT NULL,
    product_id uuid NOT NULL,
    current_quantity numeric(14,3) DEFAULT 0 NOT NULL,
    reserved_quantity numeric(14,3) DEFAULT 0 NOT NULL,
    safety_stock numeric(14,3) DEFAULT 0 NOT NULL,
    updated_by_id uuid,
    last_updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    max_quantity numeric(14,3),
    CONSTRAINT stock_reserved_not_gt_current CHECK ((reserved_quantity <= current_quantity)),
    CONSTRAINT stocks_current_quantity_check CHECK ((current_quantity >= (0)::numeric)),
    CONSTRAINT stocks_minimum_threshold_check CHECK ((safety_stock >= (0)::numeric)),
    CONSTRAINT stocks_reserved_quantity_check CHECK ((reserved_quantity >= (0)::numeric))
);


--
-- Name: support_ticket_attachments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.support_ticket_attachments (
    id uuid NOT NULL,
    message_id uuid NOT NULL,
    storage_path text NOT NULL,
    file_name character varying(255) NOT NULL,
    content_type character varying(100) NOT NULL,
    size_bytes bigint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: support_ticket_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.support_ticket_messages (
    id uuid NOT NULL,
    ticket_id uuid NOT NULL,
    sender_user_id uuid,
    sender_role character varying(20) NOT NULL,
    body text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT support_ticket_messages_sender_role_check CHECK (((sender_role)::text = ANY ((ARRAY['REQUESTER'::character varying, 'ADMIN'::character varying, 'SYSTEM'::character varying])::text[])))
);


--
-- Name: support_ticket_number_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.support_ticket_number_seq
    START WITH 1000
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: support_tickets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.support_tickets (
    id uuid NOT NULL,
    ticket_number bigint DEFAULT nextval('public.support_ticket_number_seq'::regclass) NOT NULL,
    requester_user_id uuid NOT NULL,
    assigned_admin_id uuid,
    status character varying(20) NOT NULL,
    subject character varying(255) NOT NULL,
    message text NOT NULL,
    requester_name_snapshot character varying(255) NOT NULL,
    requester_email_snapshot character varying(255) NOT NULL,
    requester_role_snapshot character varying(50),
    requester_company_snapshot character varying(255),
    requester_phone_snapshot character varying(50),
    admin_reply_message text,
    claimed_at timestamp with time zone,
    admin_replied_at timestamp with time zone,
    closed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_message_at timestamp with time zone,
    last_message_by_role character varying(20),
    CONSTRAINT support_tickets_last_message_by_role_check CHECK (((last_message_by_role IS NULL) OR ((last_message_by_role)::text = ANY ((ARRAY['REQUESTER'::character varying, 'ADMIN'::character varying, 'SYSTEM'::character varying])::text[]))))
);


--
-- Name: user_preferences; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_preferences (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    user_id uuid NOT NULL,
    alerts_in_app boolean DEFAULT true NOT NULL,
    alerts_email boolean DEFAULT true NOT NULL,
    alerts_sms boolean DEFAULT false NOT NULL,
    preferred_unit character varying(20) DEFAULT 'kg'::character varying NOT NULL,
    profile_photo character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    email character varying(255) NOT NULL,
    password character varying(128) NOT NULL,
    first_name character varying(150) NOT NULL,
    last_name character varying(150) NOT NULL,
    role character varying(20) NOT NULL,
    registration_source character varying(30) DEFAULT 'SELF_REGISTERED'::character varying NOT NULL,
    account_status character varying(40) DEFAULT 'PENDING_EMAIL_CONFIRMATION'::character varying NOT NULL,
    email_verified_at timestamp with time zone,
    is_active boolean DEFAULT false NOT NULL,
    is_staff boolean DEFAULT false NOT NULL,
    last_login timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT users_account_status_check CHECK (((account_status)::text = ANY (ARRAY[('PENDING_EMAIL_CONFIRMATION'::character varying)::text, ('ACTIVE'::character varying)::text, ('SUSPENDED'::character varying)::text]))),
    CONSTRAINT users_registration_source_check CHECK (((registration_source)::text = ANY (ARRAY[('SELF_REGISTERED'::character varying)::text, ('ADMIN_CREATED'::character varying)::text]))),
    CONSTRAINT users_role_check CHECK (((role)::text = ANY (ARRAY[('CLIENTE'::character varying)::text, ('ADMIN'::character varying)::text])))
);


--
-- Name: vision4farms_sync_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vision4farms_sync_log (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    sync_type character varying(30) NOT NULL,
    status character varying(20) NOT NULL,
    records_received integer DEFAULT 0 NOT NULL,
    records_imported integer DEFAULT 0 NOT NULL,
    records_skipped integer DEFAULT 0 NOT NULL,
    error_message text,
    payload_summary jsonb,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    CONSTRAINT vision4farms_sync_log_records_imported_check CHECK ((records_imported >= 0)),
    CONSTRAINT vision4farms_sync_log_records_received_check CHECK ((records_received >= 0)),
    CONSTRAINT vision4farms_sync_log_records_skipped_check CHECK ((records_skipped >= 0)),
    CONSTRAINT vision4farms_sync_log_status_check CHECK (((status)::text = ANY (ARRAY[('SUCCESS'::character varying)::text, ('PARTIAL'::character varying)::text, ('FAILED'::character varying)::text]))),
    CONSTRAINT vision4farms_sync_log_sync_type_check CHECK (((sync_type)::text = ANY (ARRAY[('DEFICITS'::character varying)::text, ('FORECASTS'::character varying)::text, ('NEEDS'::character varying)::text, ('EVENTS'::character varying)::text])))
);


--
-- Name: account_verification_tokens account_verification_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_verification_tokens
    ADD CONSTRAINT account_verification_tokens_pkey PRIMARY KEY (id);


--
-- Name: account_verification_tokens account_verification_tokens_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_verification_tokens
    ADD CONSTRAINT account_verification_tokens_token_key UNIQUE (token);


--
-- Name: alert_deliveries alert_deliveries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_deliveries
    ADD CONSTRAINT alert_deliveries_pkey PRIMARY KEY (id);


--
-- Name: alert_events alert_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_events
    ADD CONSTRAINT alert_events_pkey PRIMARY KEY (id);


--
-- Name: alerts alerts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alerts
    ADD CONSTRAINT alerts_pkey PRIMARY KEY (id);


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);


--
-- Name: auth_group auth_group_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group
    ADD CONSTRAINT auth_group_name_key UNIQUE (name);


--
-- Name: auth_group_permissions auth_group_permissions_group_id_permission_id_0cd325b0_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group_permissions
    ADD CONSTRAINT auth_group_permissions_group_id_permission_id_0cd325b0_uniq UNIQUE (group_id, permission_id);


--
-- Name: auth_group_permissions auth_group_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group_permissions
    ADD CONSTRAINT auth_group_permissions_pkey PRIMARY KEY (id);


--
-- Name: auth_group auth_group_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group
    ADD CONSTRAINT auth_group_pkey PRIMARY KEY (id);


--
-- Name: auth_permission auth_permission_content_type_id_codename_01ab375a_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_permission
    ADD CONSTRAINT auth_permission_content_type_id_codename_01ab375a_uniq UNIQUE (content_type_id, codename);


--
-- Name: auth_permission auth_permission_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_permission
    ADD CONSTRAINT auth_permission_pkey PRIMARY KEY (id);


--
-- Name: auth_user_groups auth_user_groups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user_groups
    ADD CONSTRAINT auth_user_groups_pkey PRIMARY KEY (id);


--
-- Name: auth_user_groups auth_user_groups_user_id_group_id_94350c0c_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user_groups
    ADD CONSTRAINT auth_user_groups_user_id_group_id_94350c0c_uniq UNIQUE (user_id, group_id);


--
-- Name: auth_user auth_user_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user
    ADD CONSTRAINT auth_user_pkey PRIMARY KEY (id);


--
-- Name: auth_user_user_permissions auth_user_user_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user_user_permissions
    ADD CONSTRAINT auth_user_user_permissions_pkey PRIMARY KEY (id);


--
-- Name: auth_user_user_permissions auth_user_user_permissions_user_id_permission_id_14a6b632_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user_user_permissions
    ADD CONSTRAINT auth_user_user_permissions_user_id_permission_id_14a6b632_uniq UNIQUE (user_id, permission_id);


--
-- Name: auth_user auth_user_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user
    ADD CONSTRAINT auth_user_username_key UNIQUE (username);


--
-- Name: conversation_participants conversation_participants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_participants
    ADD CONSTRAINT conversation_participants_pkey PRIMARY KEY (id);


--
-- Name: conversations conversations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_pkey PRIMARY KEY (id);


--
-- Name: django_admin_log django_admin_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_admin_log
    ADD CONSTRAINT django_admin_log_pkey PRIMARY KEY (id);


--
-- Name: django_content_type django_content_type_app_label_model_76bd3d3b_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_content_type
    ADD CONSTRAINT django_content_type_app_label_model_76bd3d3b_uniq UNIQUE (app_label, model);


--
-- Name: django_content_type django_content_type_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_content_type
    ADD CONSTRAINT django_content_type_pkey PRIMARY KEY (id);


--
-- Name: django_migrations django_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_migrations
    ADD CONSTRAINT django_migrations_pkey PRIMARY KEY (id);


--
-- Name: django_session django_session_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_session
    ADD CONSTRAINT django_session_pkey PRIMARY KEY (session_key);


--
-- Name: external_customer_demands external_customer_demands_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_customer_demands
    ADD CONSTRAINT external_customer_demands_pkey PRIMARY KEY (id);


--
-- Name: marketplace_listings marketplace_listings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketplace_listings
    ADD CONSTRAINT marketplace_listings_pkey PRIMARY KEY (id);


--
-- Name: messages messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (id);


--
-- Name: needs needs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.needs
    ADD CONSTRAINT needs_pkey PRIMARY KEY (id);


--
-- Name: notifications notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (id);


--
-- Name: order_groups order_groups_group_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_groups
    ADD CONSTRAINT order_groups_group_number_key UNIQUE (group_number);


--
-- Name: order_groups order_groups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_groups
    ADD CONSTRAINT order_groups_pkey PRIMARY KEY (id);


--
-- Name: order_items order_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_pkey PRIMARY KEY (id);


--
-- Name: order_status_history order_status_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_status_history
    ADD CONSTRAINT order_status_history_pkey PRIMARY KEY (id);


--
-- Name: orders orders_order_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_order_number_key UNIQUE (order_number);


--
-- Name: orders orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (id);


--
-- Name: producer_products producer_product_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_products
    ADD CONSTRAINT producer_product_unique UNIQUE (producer_id, product_id);


--
-- Name: producer_products producer_products_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_products
    ADD CONSTRAINT producer_products_pkey PRIMARY KEY (id);


--
-- Name: producer_profiles producer_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_profiles
    ADD CONSTRAINT producer_profiles_pkey PRIMARY KEY (id);


--
-- Name: producer_profiles producer_profiles_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_profiles
    ADD CONSTRAINT producer_profiles_user_id_key UNIQUE (user_id);


--
-- Name: product_categories product_categories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_categories
    ADD CONSTRAINT product_categories_pkey PRIMARY KEY (id);


--
-- Name: product_categories product_categories_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_categories
    ADD CONSTRAINT product_categories_slug_key UNIQUE (slug);


--
-- Name: production_forecasts production_forecasts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_forecasts
    ADD CONSTRAINT production_forecasts_pkey PRIMARY KEY (id);


--
-- Name: products products_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_pkey PRIMARY KEY (id);


--
-- Name: products products_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_slug_key UNIQUE (slug);


--
-- Name: recommendation_items recommendation_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recommendation_items
    ADD CONSTRAINT recommendation_items_pkey PRIMARY KEY (id);


--
-- Name: recommendations recommendations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recommendations
    ADD CONSTRAINT recommendations_pkey PRIMARY KEY (id);


--
-- Name: stock_movements stock_movements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_movements
    ADD CONSTRAINT stock_movements_pkey PRIMARY KEY (id);


--
-- Name: stocks stocks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stocks
    ADD CONSTRAINT stocks_pkey PRIMARY KEY (id);


--
-- Name: support_ticket_attachments support_ticket_attachments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_attachments
    ADD CONSTRAINT support_ticket_attachments_pkey PRIMARY KEY (id);


--
-- Name: support_ticket_messages support_ticket_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_messages
    ADD CONSTRAINT support_ticket_messages_pkey PRIMARY KEY (id);


--
-- Name: support_tickets support_tickets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_pkey PRIMARY KEY (id);


--
-- Name: support_tickets support_tickets_ticket_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_ticket_number_key UNIQUE (ticket_number);


--
-- Name: conversation_participants unique_conversation_participant; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_participants
    ADD CONSTRAINT unique_conversation_participant UNIQUE (conversation_id, user_id);


--
-- Name: stocks unique_stock; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stocks
    ADD CONSTRAINT unique_stock UNIQUE (producer_id, product_id);


--
-- Name: user_preferences user_preferences_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_preferences
    ADD CONSTRAINT user_preferences_pkey PRIMARY KEY (id);


--
-- Name: user_preferences user_preferences_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_preferences
    ADD CONSTRAINT user_preferences_user_id_key UNIQUE (user_id);


--
-- Name: users users_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_key UNIQUE (email);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: vision4farms_sync_log vision4farms_sync_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vision4farms_sync_log
    ADD CONSTRAINT vision4farms_sync_log_pkey PRIMARY KEY (id);


--
-- Name: auth_group_name_a6ea08ec_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auth_group_name_a6ea08ec_like ON public.auth_group USING btree (name varchar_pattern_ops);


--
-- Name: auth_group_permissions_group_id_b120cbf9; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auth_group_permissions_group_id_b120cbf9 ON public.auth_group_permissions USING btree (group_id);


--
-- Name: auth_group_permissions_permission_id_84c5c92e; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auth_group_permissions_permission_id_84c5c92e ON public.auth_group_permissions USING btree (permission_id);


--
-- Name: auth_permission_content_type_id_2f476e4b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auth_permission_content_type_id_2f476e4b ON public.auth_permission USING btree (content_type_id);


--
-- Name: auth_user_groups_group_id_97559544; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auth_user_groups_group_id_97559544 ON public.auth_user_groups USING btree (group_id);


--
-- Name: auth_user_groups_user_id_6a12ed8b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auth_user_groups_user_id_6a12ed8b ON public.auth_user_groups USING btree (user_id);


--
-- Name: auth_user_user_permissions_permission_id_1fbb5f2c; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auth_user_user_permissions_permission_id_1fbb5f2c ON public.auth_user_user_permissions USING btree (permission_id);


--
-- Name: auth_user_user_permissions_user_id_a95ead1b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auth_user_user_permissions_user_id_a95ead1b ON public.auth_user_user_permissions USING btree (user_id);


--
-- Name: auth_user_username_6821ab7c_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auth_user_username_6821ab7c_like ON public.auth_user USING btree (username varchar_pattern_ops);


--
-- Name: django_admin_log_content_type_id_c4bce8eb; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX django_admin_log_content_type_id_c4bce8eb ON public.django_admin_log USING btree (content_type_id);


--
-- Name: django_admin_log_user_id_c564eba6; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX django_admin_log_user_id_c564eba6 ON public.django_admin_log USING btree (user_id);


--
-- Name: django_session_expire_date_a5c62663; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX django_session_expire_date_a5c62663 ON public.django_session USING btree (expire_date);


--
-- Name: django_session_session_key_c0390e0f_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX django_session_session_key_c0390e0f_like ON public.django_session USING btree (session_key varchar_pattern_ops);


--
-- Name: external_customer_demands_created_by_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX external_customer_demands_created_by_id_idx ON public.external_customer_demands USING btree (created_by_id);


--
-- Name: external_customer_demands_generated_need_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX external_customer_demands_generated_need_id_idx ON public.external_customer_demands USING btree (generated_need_id);


--
-- Name: external_customer_demands_producer_product_delivery_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX external_customer_demands_producer_product_delivery_idx ON public.external_customer_demands USING btree (producer_id, product_id, requested_delivery_date);


--
-- Name: external_customer_demands_producer_product_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX external_customer_demands_producer_product_status_idx ON public.external_customer_demands USING btree (producer_id, product_id, status);


--
-- Name: external_customer_demands_source_external_unique_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX external_customer_demands_source_external_unique_idx ON public.external_customer_demands USING btree (producer_id, source_system, external_id) WHERE (external_id IS NOT NULL);


--
-- Name: idx_account_verification_tokens_user_purpose; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_account_verification_tokens_user_purpose ON public.account_verification_tokens USING btree (user_id, purpose);


--
-- Name: idx_alert_deliveries_alert_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_alert_deliveries_alert_channel ON public.alert_deliveries USING btree (alert_id, channel);


--
-- Name: idx_alert_deliveries_user_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_alert_deliveries_user_status ON public.alert_deliveries USING btree (user_id, status, created_at DESC);


--
-- Name: idx_alerts_producer_category_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_alerts_producer_category_status ON public.alerts USING btree (producer_id, category, status, updated_at DESC);


--
-- Name: idx_alerts_producer_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_alerts_producer_status ON public.alerts USING btree (producer_id, status);


--
-- Name: idx_alerts_producer_status_priority; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_alerts_producer_status_priority ON public.alerts USING btree (producer_id, status, priority, updated_at DESC);


--
-- Name: idx_alerts_severity_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_alerts_severity_status ON public.alerts USING btree (severity, status);


--
-- Name: idx_audit_log_user_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_audit_log_user_date ON public.audit_log USING btree (user_id, created_at DESC);


--
-- Name: idx_conversation_participants_user_archived; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversation_participants_user_archived ON public.conversation_participants USING btree (user_id, is_archived, conversation_id);


--
-- Name: idx_conversations_last_message; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversations_last_message ON public.conversations USING btree (is_active, last_message_at DESC, updated_at DESC);


--
-- Name: idx_forecasts_producer_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_forecasts_producer_product ON public.production_forecasts USING btree (producer_id, product_id);


--
-- Name: idx_listings_producer_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_listings_producer_status ON public.marketplace_listings USING btree (producer_id, status);


--
-- Name: idx_listings_product_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_listings_product_status ON public.marketplace_listings USING btree (product_id, status);


--
-- Name: idx_marketplace_listings_forecast_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_marketplace_listings_forecast_id ON public.marketplace_listings USING btree (forecast_id);


--
-- Name: idx_marketplace_listings_need_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_marketplace_listings_need_id ON public.marketplace_listings USING btree (need_id);


--
-- Name: idx_marketplace_listings_need_response_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_marketplace_listings_need_response_status ON public.marketplace_listings USING btree (need_id, need_response_status);


--
-- Name: idx_messages_conversation_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_conversation_created ON public.messages USING btree (conversation_id, created_at DESC);


--
-- Name: idx_messages_conversation_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_conversation_date ON public.messages USING btree (conversation_id, created_at DESC);


--
-- Name: idx_needs_producer_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_needs_producer_status ON public.needs USING btree (producer_id, status);


--
-- Name: idx_notifications_user_unread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_notifications_user_unread ON public.notifications USING btree (user_id, is_read) WHERE (is_read = false);


--
-- Name: idx_order_groups_buyer_producer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_order_groups_buyer_producer_id ON public.order_groups USING btree (buyer_producer_id);


--
-- Name: idx_order_items_need_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_order_items_need_id ON public.order_items USING btree (need_id);


--
-- Name: idx_order_items_seller; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_order_items_seller ON public.order_items USING btree (seller_producer_id);


--
-- Name: idx_orders_buyer_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_orders_buyer_status ON public.orders USING btree (buyer_producer_id, status);


--
-- Name: idx_orders_group_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_orders_group_id ON public.orders USING btree (group_id);


--
-- Name: idx_producer_products_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_producer_products_product ON public.producer_products USING btree (product_id);


--
-- Name: idx_product_categories_lower_name_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_product_categories_lower_name_unique ON public.product_categories USING btree (lower((name)::text));


--
-- Name: idx_products_lower_name_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_products_lower_name_unique ON public.products USING btree (lower((name)::text));


--
-- Name: idx_recommendations_need_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_recommendations_need_id ON public.recommendations USING btree (need_id);


--
-- Name: idx_recommendations_producer_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_recommendations_producer_status ON public.recommendations USING btree (producer_id, status);


--
-- Name: idx_stock_movements_stock_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stock_movements_stock_date ON public.stock_movements USING btree (stock_id, created_at DESC);


--
-- Name: idx_stocks_producer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stocks_producer ON public.stocks USING btree (producer_id);


--
-- Name: idx_support_ticket_attachments_message; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_support_ticket_attachments_message ON public.support_ticket_attachments USING btree (message_id);


--
-- Name: idx_support_ticket_messages_sender_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_support_ticket_messages_sender_role ON public.support_ticket_messages USING btree (ticket_id, sender_role, created_at);


--
-- Name: idx_support_ticket_messages_ticket_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_support_ticket_messages_ticket_created ON public.support_ticket_messages USING btree (ticket_id, created_at);


--
-- Name: idx_support_tickets_assigned_admin_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_support_tickets_assigned_admin_id ON public.support_tickets USING btree (assigned_admin_id);


--
-- Name: idx_support_tickets_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_support_tickets_created_at ON public.support_tickets USING btree (created_at DESC);


--
-- Name: idx_support_tickets_last_message_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_support_tickets_last_message_role ON public.support_tickets USING btree (status, last_message_by_role, last_message_at);


--
-- Name: idx_support_tickets_requester_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_support_tickets_requester_user_id ON public.support_tickets USING btree (requester_user_id);


--
-- Name: idx_support_tickets_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_support_tickets_status ON public.support_tickets USING btree (status);


--
-- Name: idx_sync_log_type_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sync_log_type_date ON public.vision4farms_sync_log USING btree (sync_type, started_at DESC);


--
-- Name: idx_user_preferences_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_preferences_user ON public.user_preferences USING btree (user_id);


--
-- Name: idx_users_account_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_account_status ON public.users USING btree (account_status);


--
-- Name: uniq_active_alert_context; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uniq_active_alert_context ON public.alerts USING btree (producer_id, type, context_key) WHERE (((status)::text = ANY ((ARRAY['ACTIVE'::character varying, 'READ'::character varying, 'IGNORED'::character varying, 'RESOLVED'::character varying])::text[])) AND (cleared_at IS NULL) AND (context_key IS NOT NULL));


--
-- Name: account_verification_tokens account_verification_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_verification_tokens
    ADD CONSTRAINT account_verification_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: alert_deliveries alert_deliveries_alert_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_deliveries
    ADD CONSTRAINT alert_deliveries_alert_id_fkey FOREIGN KEY (alert_id) REFERENCES public.alerts(id) ON DELETE CASCADE;


--
-- Name: alert_deliveries alert_deliveries_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_deliveries
    ADD CONSTRAINT alert_deliveries_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: alert_events alert_events_alert_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_events
    ADD CONSTRAINT alert_events_alert_id_fkey FOREIGN KEY (alert_id) REFERENCES public.alerts(id) ON DELETE CASCADE;


--
-- Name: alert_events alert_events_performed_by_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_events
    ADD CONSTRAINT alert_events_performed_by_id_fkey FOREIGN KEY (performed_by_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: alerts alerts_forecast_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alerts
    ADD CONSTRAINT alerts_forecast_id_fkey FOREIGN KEY (forecast_id) REFERENCES public.production_forecasts(id) ON DELETE SET NULL;


--
-- Name: alerts alerts_listing_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alerts
    ADD CONSTRAINT alerts_listing_id_fkey FOREIGN KEY (listing_id) REFERENCES public.marketplace_listings(id) ON DELETE SET NULL;


--
-- Name: alerts alerts_need_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alerts
    ADD CONSTRAINT alerts_need_id_fkey FOREIGN KEY (need_id) REFERENCES public.needs(id) ON DELETE SET NULL;


--
-- Name: alerts alerts_producer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alerts
    ADD CONSTRAINT alerts_producer_id_fkey FOREIGN KEY (producer_id) REFERENCES public.producer_profiles(id) ON DELETE CASCADE;


--
-- Name: alerts alerts_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alerts
    ADD CONSTRAINT alerts_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE SET NULL;


--
-- Name: audit_log audit_log_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: auth_group_permissions auth_group_permissio_permission_id_84c5c92e_fk_auth_perm; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group_permissions
    ADD CONSTRAINT auth_group_permissio_permission_id_84c5c92e_fk_auth_perm FOREIGN KEY (permission_id) REFERENCES public.auth_permission(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: auth_group_permissions auth_group_permissions_group_id_b120cbf9_fk_auth_group_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group_permissions
    ADD CONSTRAINT auth_group_permissions_group_id_b120cbf9_fk_auth_group_id FOREIGN KEY (group_id) REFERENCES public.auth_group(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: auth_permission auth_permission_content_type_id_2f476e4b_fk_django_co; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_permission
    ADD CONSTRAINT auth_permission_content_type_id_2f476e4b_fk_django_co FOREIGN KEY (content_type_id) REFERENCES public.django_content_type(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: auth_user_groups auth_user_groups_group_id_97559544_fk_auth_group_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user_groups
    ADD CONSTRAINT auth_user_groups_group_id_97559544_fk_auth_group_id FOREIGN KEY (group_id) REFERENCES public.auth_group(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: auth_user_groups auth_user_groups_user_id_6a12ed8b_fk_auth_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user_groups
    ADD CONSTRAINT auth_user_groups_user_id_6a12ed8b_fk_auth_user_id FOREIGN KEY (user_id) REFERENCES public.auth_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: auth_user_user_permissions auth_user_user_permi_permission_id_1fbb5f2c_fk_auth_perm; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user_user_permissions
    ADD CONSTRAINT auth_user_user_permi_permission_id_1fbb5f2c_fk_auth_perm FOREIGN KEY (permission_id) REFERENCES public.auth_permission(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: auth_user_user_permissions auth_user_user_permissions_user_id_a95ead1b_fk_auth_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user_user_permissions
    ADD CONSTRAINT auth_user_user_permissions_user_id_a95ead1b_fk_auth_user_id FOREIGN KEY (user_id) REFERENCES public.auth_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: conversation_participants conversation_participants_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_participants
    ADD CONSTRAINT conversation_participants_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.conversations(id) ON DELETE CASCADE;


--
-- Name: conversation_participants conversation_participants_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_participants
    ADD CONSTRAINT conversation_participants_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: conversations conversations_created_by_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_created_by_id_fkey FOREIGN KEY (created_by_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: conversations conversations_listing_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_listing_id_fkey FOREIGN KEY (listing_id) REFERENCES public.marketplace_listings(id) ON DELETE SET NULL;


--
-- Name: conversations conversations_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE SET NULL;


--
-- Name: django_admin_log django_admin_log_content_type_id_c4bce8eb_fk_django_co; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_admin_log
    ADD CONSTRAINT django_admin_log_content_type_id_c4bce8eb_fk_django_co FOREIGN KEY (content_type_id) REFERENCES public.django_content_type(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: django_admin_log django_admin_log_user_id_c564eba6_fk_auth_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_admin_log
    ADD CONSTRAINT django_admin_log_user_id_c564eba6_fk_auth_user_id FOREIGN KEY (user_id) REFERENCES public.auth_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: external_customer_demands external_customer_demands_created_by_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_customer_demands
    ADD CONSTRAINT external_customer_demands_created_by_id_fkey FOREIGN KEY (created_by_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: external_customer_demands external_customer_demands_generated_need_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_customer_demands
    ADD CONSTRAINT external_customer_demands_generated_need_id_fkey FOREIGN KEY (generated_need_id) REFERENCES public.needs(id) ON DELETE SET NULL;


--
-- Name: external_customer_demands external_customer_demands_producer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_customer_demands
    ADD CONSTRAINT external_customer_demands_producer_id_fkey FOREIGN KEY (producer_id) REFERENCES public.producer_profiles(id) ON DELETE CASCADE;


--
-- Name: external_customer_demands external_customer_demands_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_customer_demands
    ADD CONSTRAINT external_customer_demands_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE RESTRICT;


--
-- Name: external_customer_demands external_customer_demands_updated_by_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_customer_demands
    ADD CONSTRAINT external_customer_demands_updated_by_id_fkey FOREIGN KEY (updated_by_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: marketplace_listings marketplace_listings_forecast_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketplace_listings
    ADD CONSTRAINT marketplace_listings_forecast_fk FOREIGN KEY (forecast_id) REFERENCES public.production_forecasts(id) ON DELETE SET NULL;


--
-- Name: marketplace_listings marketplace_listings_need_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketplace_listings
    ADD CONSTRAINT marketplace_listings_need_id_fkey FOREIGN KEY (need_id) REFERENCES public.needs(id) ON DELETE SET NULL;


--
-- Name: marketplace_listings marketplace_listings_producer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketplace_listings
    ADD CONSTRAINT marketplace_listings_producer_id_fkey FOREIGN KEY (producer_id) REFERENCES public.producer_profiles(id) ON DELETE CASCADE;


--
-- Name: marketplace_listings marketplace_listings_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketplace_listings
    ADD CONSTRAINT marketplace_listings_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;


--
-- Name: marketplace_listings marketplace_listings_stock_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketplace_listings
    ADD CONSTRAINT marketplace_listings_stock_id_fkey FOREIGN KEY (stock_id) REFERENCES public.stocks(id) ON DELETE SET NULL;


--
-- Name: messages messages_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.conversations(id) ON DELETE CASCADE;


--
-- Name: messages messages_sender_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_sender_user_id_fkey FOREIGN KEY (sender_user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: needs needs_producer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.needs
    ADD CONSTRAINT needs_producer_id_fkey FOREIGN KEY (producer_id) REFERENCES public.producer_profiles(id) ON DELETE CASCADE;


--
-- Name: needs needs_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.needs
    ADD CONSTRAINT needs_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;


--
-- Name: notifications notifications_alert_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_alert_id_fkey FOREIGN KEY (alert_id) REFERENCES public.alerts(id) ON DELETE CASCADE;


--
-- Name: notifications notifications_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_message_id_fkey FOREIGN KEY (message_id) REFERENCES public.messages(id) ON DELETE CASCADE;


--
-- Name: notifications notifications_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE CASCADE;


--
-- Name: notifications notifications_recommendation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_recommendation_id_fkey FOREIGN KEY (recommendation_id) REFERENCES public.recommendations(id) ON DELETE CASCADE;


--
-- Name: notifications notifications_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: order_groups order_groups_buyer_producer_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_groups
    ADD CONSTRAINT order_groups_buyer_producer_fk FOREIGN KEY (buyer_producer_id) REFERENCES public.producer_profiles(id) ON DELETE RESTRICT;


--
-- Name: order_items order_items_listing_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_listing_id_fkey FOREIGN KEY (listing_id) REFERENCES public.marketplace_listings(id) ON DELETE SET NULL;


--
-- Name: order_items order_items_need_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_need_id_fkey FOREIGN KEY (need_id) REFERENCES public.needs(id) ON DELETE SET NULL;


--
-- Name: order_items order_items_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE CASCADE;


--
-- Name: order_items order_items_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE RESTRICT;


--
-- Name: order_items order_items_seller_producer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_seller_producer_id_fkey FOREIGN KEY (seller_producer_id) REFERENCES public.producer_profiles(id) ON DELETE RESTRICT;


--
-- Name: order_status_history order_status_history_changed_by_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_status_history
    ADD CONSTRAINT order_status_history_changed_by_id_fkey FOREIGN KEY (changed_by_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: order_status_history order_status_history_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_status_history
    ADD CONSTRAINT order_status_history_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE CASCADE;


--
-- Name: orders orders_buyer_producer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_buyer_producer_id_fkey FOREIGN KEY (buyer_producer_id) REFERENCES public.producer_profiles(id) ON DELETE CASCADE;


--
-- Name: orders orders_group_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_group_fk FOREIGN KEY (group_id) REFERENCES public.order_groups(id) ON DELETE SET NULL;


--
-- Name: orders orders_recommendation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_recommendation_id_fkey FOREIGN KEY (recommendation_id) REFERENCES public.recommendations(id) ON DELETE SET NULL;


--
-- Name: producer_products producer_products_producer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_products
    ADD CONSTRAINT producer_products_producer_id_fkey FOREIGN KEY (producer_id) REFERENCES public.producer_profiles(id) ON DELETE CASCADE;


--
-- Name: producer_products producer_products_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_products
    ADD CONSTRAINT producer_products_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;


--
-- Name: producer_profiles producer_profiles_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_profiles
    ADD CONSTRAINT producer_profiles_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: production_forecasts production_forecasts_producer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_forecasts
    ADD CONSTRAINT production_forecasts_producer_id_fkey FOREIGN KEY (producer_id) REFERENCES public.producer_profiles(id) ON DELETE CASCADE;


--
-- Name: production_forecasts production_forecasts_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_forecasts
    ADD CONSTRAINT production_forecasts_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;


--
-- Name: products products_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.product_categories(id) ON DELETE SET NULL;


--
-- Name: recommendation_items recommendation_items_listing_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recommendation_items
    ADD CONSTRAINT recommendation_items_listing_id_fkey FOREIGN KEY (listing_id) REFERENCES public.marketplace_listings(id) ON DELETE RESTRICT;


--
-- Name: recommendation_items recommendation_items_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recommendation_items
    ADD CONSTRAINT recommendation_items_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE RESTRICT;


--
-- Name: recommendation_items recommendation_items_recommendation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recommendation_items
    ADD CONSTRAINT recommendation_items_recommendation_id_fkey FOREIGN KEY (recommendation_id) REFERENCES public.recommendations(id) ON DELETE CASCADE;


--
-- Name: recommendation_items recommendation_items_seller_producer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recommendation_items
    ADD CONSTRAINT recommendation_items_seller_producer_id_fkey FOREIGN KEY (seller_producer_id) REFERENCES public.producer_profiles(id) ON DELETE RESTRICT;


--
-- Name: recommendations recommendations_generated_from_alert_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recommendations
    ADD CONSTRAINT recommendations_generated_from_alert_id_fkey FOREIGN KEY (generated_from_alert_id) REFERENCES public.alerts(id) ON DELETE SET NULL;


--
-- Name: recommendations recommendations_need_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recommendations
    ADD CONSTRAINT recommendations_need_id_fkey FOREIGN KEY (need_id) REFERENCES public.needs(id) ON DELETE SET NULL;


--
-- Name: recommendations recommendations_producer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recommendations
    ADD CONSTRAINT recommendations_producer_id_fkey FOREIGN KEY (producer_id) REFERENCES public.producer_profiles(id) ON DELETE CASCADE;


--
-- Name: recommendations recommendations_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recommendations
    ADD CONSTRAINT recommendations_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;


--
-- Name: stock_movements stock_movements_performed_by_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_movements
    ADD CONSTRAINT stock_movements_performed_by_id_fkey FOREIGN KEY (performed_by_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: stock_movements stock_movements_stock_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_movements
    ADD CONSTRAINT stock_movements_stock_id_fkey FOREIGN KEY (stock_id) REFERENCES public.stocks(id) ON DELETE CASCADE;


--
-- Name: stocks stocks_producer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stocks
    ADD CONSTRAINT stocks_producer_id_fkey FOREIGN KEY (producer_id) REFERENCES public.producer_profiles(id) ON DELETE CASCADE;


--
-- Name: stocks stocks_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stocks
    ADD CONSTRAINT stocks_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;


--
-- Name: stocks stocks_updated_by_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stocks
    ADD CONSTRAINT stocks_updated_by_id_fkey FOREIGN KEY (updated_by_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: support_ticket_attachments support_ticket_attachments_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_attachments
    ADD CONSTRAINT support_ticket_attachments_message_id_fkey FOREIGN KEY (message_id) REFERENCES public.support_ticket_messages(id) ON DELETE CASCADE;


--
-- Name: support_ticket_messages support_ticket_messages_sender_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_messages
    ADD CONSTRAINT support_ticket_messages_sender_user_id_fkey FOREIGN KEY (sender_user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: support_ticket_messages support_ticket_messages_ticket_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_ticket_messages
    ADD CONSTRAINT support_ticket_messages_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.support_tickets(id) ON DELETE CASCADE;


--
-- Name: support_tickets support_tickets_assigned_admin_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_assigned_admin_id_fkey FOREIGN KEY (assigned_admin_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: support_tickets support_tickets_requester_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_requester_user_id_fkey FOREIGN KEY (requester_user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: user_preferences user_preferences_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_preferences
    ADD CONSTRAINT user_preferences_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict pdTPoMqzpYQWMth4tieBkfo280mXoCPDlAWZgzGx8ORHNjN511hk8YT4XsgJbeA

