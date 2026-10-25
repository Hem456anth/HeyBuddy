package com.heybuddy.android.activation

import android.content.Context
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.Image
import androidx.glance.ImageProvider
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.GlanceAppWidgetReceiver
import androidx.glance.appwidget.action.actionStartActivity
import androidx.glance.appwidget.cornerRadius
import androidx.glance.appwidget.provideContent
import androidx.glance.background
import androidx.glance.layout.Alignment
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.padding
import androidx.glance.layout.size
import androidx.glance.layout.width
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import com.heybuddy.android.R
import com.heybuddy.android.ui.CompanionActivity

/** Home-screen widget: a single "Talk to HeyBuddy" pill. */
class TalkWidget : GlanceAppWidget() {
    override suspend fun provideGlance(context: Context, id: GlanceId) {
        val intent = CompanionActivity.talkIntent(context, source = "widget")
        provideContent {
            Row(
                modifier = GlanceModifier.fillMaxSize().cornerRadius(28.dp).background(Color(0xFF15161A)).padding(horizontal = 16.dp)
                    .clickable(actionStartActivity(intent)),
                verticalAlignment = Alignment.CenterVertically,
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Image(ImageProvider(R.drawable.ic_buddy), contentDescription = null, modifier = GlanceModifier.size(22.dp))
                Spacer(GlanceModifier.width(10.dp))
                Text("Talk to HeyBuddy", style = TextStyle(color = ColorProvider(Color.White), fontSize = 15.sp, fontWeight = FontWeight.Medium))
            }
        }
    }
}

class TalkWidgetReceiver : GlanceAppWidgetReceiver() {
    override val glanceAppWidget: GlanceAppWidget = TalkWidget()
}
